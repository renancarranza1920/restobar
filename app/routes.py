import csv
from datetime import date, datetime
from io import BytesIO, StringIO
from pathlib import Path
from uuid import uuid4

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    current_user,
    fresh_login_required,
    login_fresh,
    login_required,
    login_user,
    logout_user,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .extensions import db
from .models import (
    Categoria,
    Mesa,
    MovimientoCaja,
    MovimientoInventario,
    Orden,
    OrdenDivision,
    OrdenItem,
    Pago,
    Producto,
    SesionCaja,
    Usuario,
    Zona,
)
from .services import (
    bool_from_form,
    build_split_matrix,
    clear_divisiones,
    default_endpoint_for_user,
    default_theme,
    division_can_receive_payment,
    feature_required,
    get_active_cash_session,
    get_active_order_for_mesa,
    get_categorias,
    get_dashboard_metrics,
    get_dashboard_snapshot,
    get_inventory_products,
    get_low_stock_products,
    get_mesas_disponibles,
    get_order,
    get_orders_for_listing,
    get_orders_for_range,
    get_payments_for_range,
    get_pending_kitchen_items,
    get_producto,
    get_productos,
    get_report_snapshot,
    get_user,
    get_user_by_nickname,
    get_inventory_for_range,
    get_zona,
    get_zonas,
    grouped_tables,
    initial_item_status,
    item_can_be_delivered,
    item_can_be_prepared,
    normalize_item_delivery_states,
    order_can_receive_payment,
    parse_date_value,
    parse_decimal,
    parse_int,
    recent_cash_movements,
    recent_inventory_movements,
    reset_divisiones_if_possible,
    roles_required,
    save_split_configuration,
    serialize_kitchen_item,
    session_card_total,
    session_cash_expected,
    session_sales_total,
    settle_order,
    sync_order,
    theme_choices,
    user_can,
    validate_split_assignment,
)


web_bp = Blueprint("web", __name__)
api_bp = Blueprint("api", __name__, url_prefix="/api")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def database_error_response(exc):
    return (
        jsonify(
            {
                "status": "error",
                "message": "No se pudo consultar la base de datos",
                "detail": str(exc),
            }
        ),
        500,
    )


def api_permission_denied():
    return jsonify({"status": "error", "message": "No autorizado"}), 403


def parse_date_filter(raw_value):
    return parse_date_value(raw_value, date.today())


def parse_report_range():
    today = date.today()
    default_start = today.replace(day=1)
    start_date = parse_date_value(request.args.get("desde"), default_start)
    end_date = parse_date_value(request.args.get("hasta"), today)
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def flash_form_errors(errors):
    for error in errors:
        flash(error, "error")


def get_item_or_redirect(item_id):
    item = (
        OrdenItem.query.options(
            joinedload(OrdenItem.producto).joinedload(Producto.categoria),
            joinedload(OrdenItem.orden)
            .joinedload(Orden.mesa)
            .joinedload(Mesa.zona),
            joinedload(OrdenItem.orden).joinedload(Orden.usuario),
        )
        .filter_by(id=item_id)
        .first()
    )
    if item is None:
        flash("El item solicitado no existe.", "error")
        return None, redirect(url_for("web.ordenes"))
    return item, None


def valid_image_reference(value):
    if not value:
        return True
    return value.startswith("http://") or value.startswith("https://") or value.startswith(
        "/static/"
    )


def save_product_image(file_storage):
    if file_storage is None or not file_storage.filename:
        return None, None

    filename = secure_filename(file_storage.filename)
    extension = Path(filename).suffix.lower()

    if extension not in IMAGE_EXTENSIONS:
        return None, "La imagen debe ser JPG, PNG, WEBP o GIF."

    upload_dir = current_app.config["PRODUCT_UPLOAD_DIR"]
    upload_dir.mkdir(parents=True, exist_ok=True)

    generated_name = f"{datetime.utcnow():%Y%m%d%H%M%S}_{uuid4().hex[:10]}{extension}"
    destination = upload_dir / generated_name
    file_storage.save(destination)
    return f"/static/uploads/products/{generated_name}", None


def delete_uploaded_product_image(image_url):
    if not image_url or not image_url.startswith("/static/uploads/products/"):
        return

    relative_name = image_url.replace("/static/uploads/products/", "", 1)
    file_path = current_app.config["PRODUCT_UPLOAD_DIR"] / relative_name
    if file_path.exists():
        file_path.unlink(missing_ok=True)


def extract_product_payload(existing=None):
    image_url = (request.form.get("image_url") or "").strip()
    uploaded_image, upload_error = save_product_image(request.files.get("image_file"))

    name = (request.form.get("name") or "").strip()
    category_id = parse_int(request.form.get("category_id"))
    cost_price = parse_decimal(request.form.get("cost_price"))
    sale_price = parse_decimal(request.form.get("sale_price"))
    purchase_unit = (request.form.get("purchase_unit") or "unidad").strip() or "unidad"
    units_per_package = max(parse_int(request.form.get("units_per_package"), 1), 1)
    current_stock = parse_int(request.form.get("current_stock"), 0)
    manages_stock = bool_from_form(request.form.get("manages_stock"))
    available = bool_from_form(request.form.get("available"))
    remove_image = bool_from_form(request.form.get("remove_image"))

    category = db.session.get(Categoria, category_id)
    errors = []

    if category and category.envia_a_cocina:
        cost_price = 0
        purchase_unit = "unidad"
        units_per_package = 1
        manages_stock = False
        current_stock = 0

    if upload_error:
        errors.append(upload_error)
    if not valid_image_reference(image_url):
        errors.append("La URL de imagen debe iniciar con http://, https:// o /static/.")
    if not name:
        errors.append("El producto necesita un nombre.")
    if category is None:
        errors.append("La categoria seleccionada no existe.")
    if sale_price <= 0:
        errors.append("El precio de venta debe ser mayor que cero.")
    if current_stock < 0:
        errors.append("El stock inicial no puede ser negativo.")

    if errors and uploaded_image:
        delete_uploaded_product_image(uploaded_image)

    final_image = existing.imagen_url if existing else None
    if remove_image:
        final_image = None
    if image_url:
        final_image = image_url
    if uploaded_image:
        final_image = uploaded_image

    payload = {
        "nombre": name,
        "imagen_url": final_image,
        "categoria_id": category.id if category else None,
        "precio_costo": cost_price,
        "precio_venta": sale_price,
        "unidad_compra": purchase_unit,
        "unidades_por_paquete": units_per_package,
        "stock_actual": current_stock,
        "maneja_stock": manages_stock,
        "disponible": available,
    }
    return payload, errors


def build_csv_response(filename, headers, rows):
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    payload = "\ufeff" + buffer.getvalue()
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def build_pdf_response(filename, start_date, end_date, report):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=34,
        rightMargin=34,
        topMargin=38,
        bottomMargin=38,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=19,
        leading=24,
        textColor=colors.HexColor("#1f3b57"),
        spaceAfter=4,
        alignment=0,
    )

    subtitle_style = ParagraphStyle(
        "ReportSubtitle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#5b6b7a"),
        spaceAfter=12,
    )

    section_style = ParagraphStyle(
        "ReportSection",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=14,
        textColor=colors.HexColor("#274b6b"),
        spaceBefore=14,
        spaceAfter=8,
    )

    body_style = ParagraphStyle(
        "ReportBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#334155"),
    )

    small_style = ParagraphStyle(
        "SmallNote",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#64748b"),
    )

    def money(value):
        return f"${value:,.2f}"

    summary_rows = [
        ["Ventas totales", money(report["sales_total"])],
        ["Utilidad estimada", money(report["gross_profit"])],
        ["Ticket promedio", money(report["average_ticket"])],
        ["Órdenes pagadas", str(report["paid_orders_count"])],
        ["Órdenes abiertas", str(report["open_orders_count"])],
        ["Órdenes canceladas", str(report["cancelled_orders_count"])],
        ["Items vendidos", str(report["items_sold"])],
        ["Efectivo / Tarjeta", f"{money(report['cash_total'])} / {money(report['card_total'])}"],
    ]

    top_rows = [["Producto", "Unidades", "Ventas", "Utilidad"]]
    for row in report["top_products"][:10]:
        top_rows.append(
            [
                row["product"].nombre,
                str(row["quantity"]),
                money(row["sales"]),
                money(row["profit"]),
            ]
        )

    order_rows = [["Orden", "Mesa", "Cliente", "Estado", "Total"]]
    for order in report["orders"][:12]:
        order_rows.append(
            [
                f"#{order.id}",
                order.mesa.etiqueta if order.mesa else "-",
                order.nombre_cliente or "-",
                order.estado.capitalize(),
                money(order.total),
            ]
        )

    stock_rows = [["Producto", "Categoría", "Stock"]]
    for product in report["low_stock_products"][:10]:
        stock_rows.append(
            [
                product.nombre,
                product.categoria.nombre if product.categoria else "-",
                str(product.stock_actual),
            ]
        )

    def style_summary_table(table):
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#d7e2ec")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#e3ebf3")),
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                    ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#243b53")),
                    ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#334155")),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("LEADING", (0, 0), (-1, -1), 12),
                    ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f8fbfd")]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        return table

    def style_data_table(table, align_right_cols=None):
        align_right_cols = align_right_cols or []

        commands = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf1f7")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1f3b57")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.7),
            ("LEADING", (0, 0), (-1, -1), 11),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#d7e2ec")),
            ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#e3ebf3")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbfdff")]),
            ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#334155")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]

        for col in align_right_cols:
            commands.append(("ALIGN", (col, 1), (col, -1), "RIGHT"))
            commands.append(("ALIGN", (col, 0), (col, 0), "RIGHT"))

        table.setStyle(TableStyle(commands))
        return table

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    story = [
        Paragraph("Restobar", title_style),
        Paragraph("Reporte ejecutivo", title_style),
        Paragraph(
            f"Período analizado: {start_date.isoformat()} al {end_date.isoformat()}",
            subtitle_style,
        ),
        Paragraph(
            f"Documento generado automáticamente el {generated_at}.",
            small_style,
        ),
        Spacer(1, 12),
        Paragraph("Resumen general", section_style),
        style_summary_table(Table(summary_rows, colWidths=[190, 302])),
    ]

    if len(top_rows) > 1:
        story.extend(
            [
                Spacer(1, 8),
                Paragraph("Productos más vendidos", section_style),
                style_data_table(
                    Table(top_rows, colWidths=[220, 72, 95, 95]),
                    align_right_cols=[1, 2, 3],
                ),
            ]
        )

    if len(order_rows) > 1:
        story.extend(
            [
                Spacer(1, 8),
                Paragraph("Órdenes del período", section_style),
                style_data_table(
                    Table(order_rows, colWidths=[52, 82, 170, 78, 90]),
                    align_right_cols=[4],
                ),
            ]
        )

    if len(stock_rows) > 1:
        story.extend(
            [
                Spacer(1, 8),
                Paragraph("Productos con stock bajo", section_style),
                style_data_table(
                    Table(stock_rows, colWidths=[220, 200, 72]),
                    align_right_cols=[2],
                ),
            ]
        )

    def draw_page(canvas, doc):
        canvas.saveState()

        page_width, page_height = LETTER

        canvas.setStrokeColor(colors.HexColor("#d7e2ec"))
        canvas.setLineWidth(0.8)
        canvas.line(doc.leftMargin, page_height - 26, page_width - doc.rightMargin, page_height - 26)

        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(colors.HexColor("#274b6b"))
        canvas.drawString(doc.leftMargin, page_height - 20, "RESTOBAR · REPORTE EJECUTIVO")

        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawRightString(
            page_width - doc.rightMargin,
            page_height - 20,
            f"Página {canvas.getPageNumber()}",
        )

        canvas.line(doc.leftMargin, 22, page_width - doc.rightMargin, 22)
        canvas.setFont("Helvetica", 7.8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(doc.leftMargin, 11, f"Período: {start_date.isoformat()} a {end_date.isoformat()}")
        canvas.drawRightString(page_width - doc.rightMargin, 11, "Sistema Restobar")

        canvas.restoreState()

    doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)

    payload = buffer.getvalue()
    buffer.close()

    return Response(
        payload,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

def allow_ticket_access():
    return current_user.is_authenticated and (
        user_can(current_user, "ordenes") or user_can(current_user, "cocina")
    )


@web_bp.get("/")
@login_required
def home():
    return redirect(url_for(default_endpoint_for_user(current_user)))


@web_bp.get("/login")
def login():
    reauth = current_user.is_authenticated and not login_fresh()
    if current_user.is_authenticated and not reauth:
        return redirect(url_for(default_endpoint_for_user(current_user)))
    return render_template(
        "login.html",
        page_title="Confirmar sesion" if reauth else "Iniciar sesion",
        reauth=reauth,
        next_url=request.args.get("next", ""),
    )


@web_bp.post("/login")
def login_submit():
    reauth = current_user.is_authenticated and not login_fresh()
    if current_user.is_authenticated and not reauth:
        return redirect(url_for(default_endpoint_for_user(current_user)))

    nickname = (request.form.get("nickname") or "").strip()
    password = request.form.get("password") or ""
    remember = bool_from_form(request.form.get("remember"))

    if reauth:
        user = current_user
        if nickname and nickname != current_user.nickname:
            flash("Debes confirmar la sesion con tu mismo usuario.", "error")
            return render_template(
                "login.html",
                page_title="Confirmar sesion",
                reauth=True,
                next_url=request.form.get("next", ""),
            ), 401
    else:
        user = get_user_by_nickname(nickname)

    if user is None or not user.activo or not user.check_password(password):
        flash("Credenciales invalidas. Revisa usuario y contrasena.", "error")
        return render_template(
            "login.html",
            page_title="Confirmar sesion" if reauth else "Iniciar sesion",
            reauth=reauth,
            next_url=request.form.get("next", ""),
        ), 401

    if user.uses_legacy_plaintext_password:
        user.set_password(password)
        db.session.commit()

    login_user(user, remember=remember if not reauth else False, force=reauth, fresh=True)
    session["theme"] = session.get("theme", default_theme())

    if reauth:
        flash("Sesion confirmada.", "success")
    elif (
        user.nickname == current_app.config["DEFAULT_ADMIN_NICKNAME"]
        and password == current_app.config["DEFAULT_ADMIN_PASSWORD"]
    ):
        flash(
            "Entraste con la contrasena inicial del administrador. Cambiala desde Usuarios.",
            "info",
        )

    from .services import next_url_or_default

    return redirect(next_url_or_default(default_endpoint_for_user(user)))


@web_bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("Tu sesion se cerro correctamente.", "success")
    return redirect(url_for("web.login"))


@web_bp.post("/tema")
def cambiar_tema():
    current_theme = session.get("theme", default_theme())
    requested = (request.form.get("theme") or "toggle").strip().lower()

    if requested == "toggle":
        next_theme = "dark" if current_theme == "light" else "light"
    else:
        next_theme = requested if requested in theme_choices() else default_theme()

    session["theme"] = next_theme
    return redirect(request.referrer or url_for("web.login"))


@web_bp.get("/dashboard")
@feature_required("dashboard")
def dashboard():
    snapshot = get_dashboard_snapshot()
    return render_template("dashboard.html", page_title="Inicio", **snapshot)


@web_bp.get("/zonas")
@roles_required("dueño")
def zonas():
    return render_template(
        "zones.html",
        page_title="Zonas",
        zones=get_zonas(),
    )


@web_bp.get("/zonas/nueva")
@roles_required("dueño")
def nueva_zona():
    return render_template("zone_form.html", page_title="Nueva zona", zone=None)


@web_bp.get("/zonas/<int:zone_id>/editar")
@roles_required("dueño")
def editar_zona(zone_id):
    zone = get_zona(zone_id)
    if zone is None:
        flash("La zona no existe.", "error")
        return redirect(url_for("web.zonas"))
    return render_template("zone_form.html", page_title="Editar zona", zone=zone)


@web_bp.post("/zonas")
@roles_required("dueño")
def crear_zona():
    nombre = (request.form.get("nombre") or "").strip()
    if not nombre:
        flash("La zona necesita un nombre.", "error")
        return redirect(url_for("web.nueva_zona"))

    existing = Zona.query.filter(db.func.lower(Zona.nombre) == nombre.lower()).first()
    if existing:
        flash("Ya existe una zona con ese nombre.", "error")
        return redirect(url_for("web.nueva_zona"))

    zone = Zona(nombre=nombre)
    db.session.add(zone)
    db.session.commit()
    flash(f"Se creó la zona {zone.nombre}.", "success")
    return redirect(url_for("web.zonas"))


@web_bp.post("/zonas/<int:zone_id>")
@roles_required("dueño")
def actualizar_zona(zone_id):
    zone = get_zona(zone_id)
    if zone is None:
        flash("La zona no existe.", "error")
        return redirect(url_for("web.zonas"))

    nombre = (request.form.get("nombre") or "").strip()
    if not nombre:
        flash("La zona necesita un nombre.", "error")
        return redirect(url_for("web.editar_zona", zone_id=zone.id))

    existing = (
        Zona.query.filter(db.func.lower(Zona.nombre) == nombre.lower(), Zona.id != zone.id)
        .first()
    )
    if existing:
        flash("Ya existe otra zona con ese nombre.", "error")
        return redirect(url_for("web.editar_zona", zone_id=zone.id))

    zone.nombre = nombre
    db.session.commit()
    flash(f"Se actualizó la zona {zone.nombre}.", "success")
    return redirect(url_for("web.zonas"))


@web_bp.post("/zonas/<int:zone_id>/eliminar")
@roles_required("dueño")
def eliminar_zona(zone_id):
    zone = get_zona(zone_id)
    if zone is None:
        flash("La zona no existe.", "error")
        return redirect(url_for("web.zonas"))
    if zone.mesas:
        flash("No puedes borrar una zona que todavía tiene mesas asociadas.", "warning")
        return redirect(url_for("web.zonas"))

    zone_name = zone.nombre
    db.session.delete(zone)
    db.session.commit()
    flash(f"La zona {zone_name} fue eliminada.", "success")
    return redirect(url_for("web.zonas"))


@web_bp.get("/categorias")
@feature_required("categorias")
def categorias():
    return render_template(
        "categories.html",
        page_title="Categorias",
        categories=get_categorias(),
    )


@web_bp.get("/categorias/nueva")
@feature_required("categorias")
def nueva_categoria():
    return render_template(
        "category_form.html",
        page_title="Nueva categoria",
        category=None,
    )


@web_bp.get("/categorias/<int:category_id>/editar")
@feature_required("categorias")
def editar_categoria(category_id):
    category = db.session.get(Categoria, category_id)
    if category is None:
        flash("La categoria no existe.", "error")
        return redirect(url_for("web.categorias"))
    return render_template(
        "category_form.html",
        page_title=f"Editar {category.nombre}",
        category=category,
    )


@web_bp.post("/categorias")
@feature_required("categorias")
def crear_categoria():
    nombre = (request.form.get("nombre") or "").strip()
    envia_a_cocina = bool_from_form(request.form.get("envia_a_cocina"))

    if not nombre:
        flash("La categoria necesita un nombre.", "error")
        return redirect(url_for("web.nueva_categoria"))

    existing = Categoria.query.filter(db.func.lower(Categoria.nombre) == nombre.lower()).first()
    if existing:
        flash("Ya existe una categoria con ese nombre.", "error")
        return redirect(url_for("web.nueva_categoria"))

    category = Categoria(nombre=nombre, envia_a_cocina=envia_a_cocina)
    db.session.add(category)
    db.session.commit()
    flash(f"Se creo la categoria {category.nombre}.", "success")
    return redirect(url_for("web.categorias"))


@web_bp.post("/categorias/<int:category_id>")
@feature_required("categorias")
def actualizar_categoria(category_id):
    category = db.session.get(Categoria, category_id)
    if category is None:
        flash("La categoria no existe.", "error")
        return redirect(url_for("web.categorias"))

    nombre = (request.form.get("nombre") or "").strip()
    envia_a_cocina = bool_from_form(request.form.get("envia_a_cocina"))

    if not nombre:
        flash("La categoria necesita un nombre.", "error")
        return redirect(url_for("web.editar_categoria", category_id=category.id))

    existing = (
        Categoria.query.filter(
            db.func.lower(Categoria.nombre) == nombre.lower(),
            Categoria.id != category.id,
        ).first()
    )
    if existing:
        flash("Ya existe otra categoria con ese nombre.", "error")
        return redirect(url_for("web.editar_categoria", category_id=category.id))

    category.nombre = nombre
    category.envia_a_cocina = envia_a_cocina
    db.session.commit()
    flash(f"Se actualizo la categoria {category.nombre}.", "success")
    return redirect(url_for("web.categorias"))


@web_bp.post("/categorias/<int:category_id>/eliminar")
@feature_required("categorias")
def eliminar_categoria(category_id):
    category = db.session.get(Categoria, category_id)
    if category is None:
        flash("La categoria no existe.", "error")
        return redirect(url_for("web.categorias"))
    if category.productos:
        flash(
            "No puedes borrar una categoria que todavia tiene productos asociados.",
            "warning",
        )
        return redirect(url_for("web.categorias"))

    category_name = category.nombre
    db.session.delete(category)
    db.session.commit()
    flash(f"La categoria {category_name} fue eliminada.", "success")
    return redirect(url_for("web.categorias"))


@web_bp.get("/usuarios")
@fresh_login_required
@feature_required("usuarios")
def usuarios():
    users = Usuario.query.order_by(Usuario.activo.desc(), Usuario.nombre.asc()).all()
    return render_template("users.html", page_title="Usuarios", users=users)


@web_bp.get("/usuarios/nuevo")
@fresh_login_required
@feature_required("usuarios")
def nuevo_usuario():
    return render_template("user_form.html", page_title="Nuevo usuario", user=None)


@web_bp.get("/usuarios/<int:user_id>/editar")
@fresh_login_required
@feature_required("usuarios")
def editar_usuario(user_id):
    user = get_user(user_id)
    if user is None:
        flash("El usuario no existe.", "error")
        return redirect(url_for("web.usuarios"))
    return render_template("user_form.html", page_title="Editar usuario", user=user)


@web_bp.post("/usuarios")
@fresh_login_required
@feature_required("usuarios")
def crear_usuario():
    nickname = (request.form.get("nickname") or "").strip()
    nombre = (request.form.get("nombre") or "").strip()
    apellido = (request.form.get("apellido") or "").strip()
    rol = request.form.get("rol")
    password = request.form.get("password") or ""
    activo = bool_from_form(request.form.get("activo"))

    errors = []
    if not nickname:
        errors.append("El usuario necesita un nickname.")
    if not nombre:
        errors.append("El usuario necesita un nombre.")
    if not apellido:
        errors.append("El usuario necesita un apellido.")
    if rol not in {"dueño", "cajero", "mesero", "cocina"}:
        errors.append("El rol seleccionado no es valido.")
    if len(password) < 6:
        errors.append("La contrasena debe tener al menos 6 caracteres.")
    if get_user_by_nickname(nickname):
        errors.append("Ese nickname ya esta en uso.")

    if errors:
        flash_form_errors(errors)
        return redirect(url_for("web.nuevo_usuario"))

    user = Usuario(
        nickname=nickname,
        nombre=nombre,
        apellido=apellido,
        rol=rol,
        activo=activo,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    flash(f"Se creo el usuario {user.nombre_completo}.", "success")
    return redirect(url_for("web.usuarios"))


@web_bp.post("/usuarios/<int:user_id>")
@fresh_login_required
@feature_required("usuarios")
def actualizar_usuario(user_id):
    user = get_user(user_id)
    if user is None:
        flash("El usuario no existe.", "error")
        return redirect(url_for("web.usuarios"))

    nickname = (request.form.get("nickname") or "").strip()
    nombre = (request.form.get("nombre") or "").strip()
    apellido = (request.form.get("apellido") or "").strip()
    rol = request.form.get("rol")
    password = request.form.get("password") or ""
    activo = bool_from_form(request.form.get("activo"))

    errors = []
    other = Usuario.query.filter(Usuario.nickname == nickname, Usuario.id != user.id).first()
    if other:
        errors.append("Ese nickname ya le pertenece a otra persona.")
    if rol not in {"dueño", "cajero", "mesero", "cocina"}:
        errors.append("El rol seleccionado no es valido.")
    if current_user.id == user.id and not activo:
        errors.append("No puedes desactivar tu propia cuenta.")
    if current_user.id == user.id and rol != "dueño":
        errors.append("No puedes quitarte a ti mismo el rol de administrador.")

    if errors:
        flash_form_errors(errors)
        return redirect(url_for("web.editar_usuario", user_id=user.id))

    user.nickname = nickname or user.nickname
    user.nombre = nombre or user.nombre
    user.apellido = apellido or user.apellido
    user.rol = rol
    user.activo = activo

    if password:
        if len(password) < 6:
            flash("La nueva contrasena debe tener al menos 6 caracteres.", "error")
            return redirect(url_for("web.editar_usuario", user_id=user.id))
        user.set_password(password)

    db.session.commit()
    flash(f"Se actualizo el usuario {user.nombre_completo}.", "success")
    return redirect(url_for("web.usuarios"))


@web_bp.post("/usuarios/<int:user_id>/eliminar")
@fresh_login_required
@feature_required("usuarios")
def eliminar_usuario(user_id):
    user = get_user(user_id)
    if user is None:
        flash("El usuario no existe.", "error")
        return redirect(url_for("web.usuarios"))
    if current_user.id == user.id:
        flash("No puedes borrar tu propia cuenta.", "warning")
        return redirect(url_for("web.usuarios"))
    if user.nickname == current_app.config["DEFAULT_ADMIN_NICKNAME"]:
        flash("No puedes borrar la cuenta base del administrador.", "warning")
        return redirect(url_for("web.usuarios"))
    if user.sesiones_caja or user.ordenes or user.movimientos_inventario:
        flash("No se puede borrar este usuario porque ya tiene movimientos registrados.", "warning")
        return redirect(url_for("web.usuarios"))

    user_name = user.nombre_completo
    db.session.delete(user)
    db.session.commit()
    flash(f"Se eliminó el usuario {user_name}.", "success")
    return redirect(url_for("web.usuarios"))


@web_bp.get("/mesas")
@feature_required("mesas")
def mesas():
    zonas, active_orders = grouped_tables()
    return render_template(
        "tables.html",
        page_title="Mesas",
        zonas=zonas,
        active_orders=active_orders,
        cash_session=get_active_cash_session(),
    )


@web_bp.get("/mesas/nueva")
@roles_required("dueño")
def nueva_mesa():
    return render_template(
        "table_form.html",
        page_title="Nueva mesa",
        mesa=None,
        zonas=get_zonas(),
    )


@web_bp.post("/mesas")
@roles_required("dueño")
def crear_mesa():
    numero = parse_int(request.form.get("numero"))
    nombre_alias = (request.form.get("nombre_alias") or "").strip()
    zona_id = parse_int(request.form.get("zona_id"))
    limpieza_estado = request.form.get("limpieza_estado") or "limpia"

    zona = db.session.get(Zona, zona_id)
    if numero <= 0 or zona is None:
        flash("Completa un numero valido y una zona existente.", "error")
        return redirect(url_for("web.nueva_mesa"))
    if limpieza_estado not in {"limpia", "sucia"}:
        flash("El estado de limpieza no es válido.", "error")
        return redirect(url_for("web.nueva_mesa"))

    exists = Mesa.query.filter_by(numero=numero, zona_id=zona.id).first()
    if exists:
        flash("Ya existe una mesa con ese numero en esa zona.", "error")
        return redirect(url_for("web.nueva_mesa"))

    mesa = Mesa(
        numero=numero,
        nombre_alias=nombre_alias or None,
        zona_id=zona.id,
        limpieza_estado=limpieza_estado,
    )
    db.session.add(mesa)
    db.session.commit()

    flash(f"Se creo {mesa.etiqueta}.", "success")
    return redirect(url_for("web.mesas"))


@web_bp.get("/mesas/<int:mesa_id>/editar")
@roles_required("dueño")
def editar_mesa(mesa_id):
    mesa = db.session.get(Mesa, mesa_id)
    if mesa is None:
        flash("La mesa no existe.", "error")
        return redirect(url_for("web.mesas"))
    return render_template(
        "table_form.html",
        page_title=f"Editar {mesa.etiqueta}",
        mesa=mesa,
        zonas=get_zonas(),
    )


@web_bp.post("/mesas/<int:mesa_id>")
@roles_required("dueño")
def actualizar_mesa(mesa_id):
    mesa = db.session.get(Mesa, mesa_id)
    if mesa is None:
        flash("La mesa no existe.", "error")
        return redirect(url_for("web.mesas"))

    numero = parse_int(request.form.get("numero"))
    nombre_alias = (request.form.get("nombre_alias") or "").strip()
    zona_id = parse_int(request.form.get("zona_id"))
    limpieza_estado = request.form.get("limpieza_estado") or mesa.limpieza_estado
    zona = db.session.get(Zona, zona_id)

    if numero <= 0 or zona is None:
        flash("Completa un numero valido y una zona existente.", "error")
        return redirect(url_for("web.editar_mesa", mesa_id=mesa.id))
    if limpieza_estado not in {"limpia", "sucia"}:
        flash("El estado de limpieza no es válido.", "error")
        return redirect(url_for("web.editar_mesa", mesa_id=mesa.id))

    exists = (
        Mesa.query.filter(Mesa.id != mesa.id)
        .filter(Mesa.numero == numero, Mesa.zona_id == zona.id)
        .first()
    )
    if exists:
        flash("Ya existe otra mesa con ese numero en esa zona.", "error")
        return redirect(url_for("web.editar_mesa", mesa_id=mesa.id))

    mesa.numero = numero
    mesa.nombre_alias = nombre_alias or None
    mesa.zona_id = zona.id
    mesa.limpieza_estado = limpieza_estado
    db.session.commit()

    flash(f"Se actualizo {mesa.etiqueta}.", "success")
    return redirect(url_for("web.mesas"))


@web_bp.post("/mesas/<int:mesa_id>/limpieza")
@roles_required("dueño", "mesero")
def actualizar_limpieza_mesa(mesa_id):
    mesa = db.session.get(Mesa, mesa_id)
    if mesa is None:
        flash("La mesa no existe.", "error")
        return redirect(url_for("web.mesas"))

    limpieza_estado = request.form.get("limpieza_estado") or "limpia"
    if limpieza_estado not in {"limpia", "sucia"}:
        flash("El estado de limpieza no es válido.", "error")
        return redirect(request.referrer or url_for("web.mesas"))
    if mesa.estado == "ocupada" and limpieza_estado == "limpia":
        flash("No puedes marcar como limpia una mesa que todavía está ocupada.", "warning")
        return redirect(request.referrer or url_for("web.mesas"))

    mesa.limpieza_estado = limpieza_estado
    db.session.commit()
    flash(
        f"{mesa.etiqueta} quedó marcada como {'limpia' if limpieza_estado == 'limpia' else 'sucia'}.",
        "success",
    )
    return redirect(request.referrer or url_for("web.mesas"))


@web_bp.post("/mesas/<int:mesa_id>/eliminar")
@roles_required("dueño")
def eliminar_mesa(mesa_id):
    mesa = db.session.get(Mesa, mesa_id)
    if mesa is None:
        flash("La mesa no existe.", "error")
        return redirect(url_for("web.mesas"))
    if get_active_order_for_mesa(mesa.id):
        flash("No puedes borrar una mesa que tiene una orden abierta.", "warning")
        return redirect(url_for("web.mesas"))
    if mesa.ordenes:
        flash("No puedes borrar una mesa que ya tiene historial de órdenes.", "warning")
        return redirect(url_for("web.mesas"))

    mesa_label = mesa.etiqueta
    db.session.delete(mesa)
    db.session.commit()
    flash(f"Se eliminó {mesa_label}.", "success")
    return redirect(url_for("web.mesas"))


@web_bp.get("/ordenes")
@feature_required("ordenes")
def ordenes():
    status = request.args.get("estado") or None
    filter_date = parse_date_filter(request.args.get("fecha"))
    orders = get_orders_for_listing(status=status, date_value=filter_date)
    available_tables = get_mesas_disponibles()
    return render_template(
        "orders.html",
        page_title="Ordenes",
        orders=orders,
        available_tables=available_tables,
        selected_status=status or "",
        selected_date=filter_date.isoformat(),
        cash_session=get_active_cash_session(),
    )


@web_bp.post("/ordenes")
@roles_required("dueño", "cajero", "mesero")
def crear_orden():
    mesa_id = parse_int(request.form.get("mesa_id"))
    if mesa_id <= 0:
        mesa_id = parse_int(request.form.get("mesa_id_llevar"))
    nombre_cliente = (request.form.get("nombre_cliente") or "").strip()
    is_takeout = mesa_id == current_app.config["TAKEOUT_TABLE_ID"]

    cash_session = get_active_cash_session()
    if cash_session is None:
        flash("Primero abre una caja para poder crear ordenes.", "error")
        return redirect(url_for("web.caja"))

    mesa = db.session.get(Mesa, mesa_id)
    if mesa is None:
        flash("La mesa seleccionada no existe.", "error")
        return redirect(request.referrer or url_for("web.ordenes"))
    if is_takeout and not nombre_cliente:
        flash("Para llevar requiere el nombre del cliente.", "error")
        return redirect(request.referrer or url_for("web.ordenes"))
    if not is_takeout and mesa.limpieza_estado == "sucia":
        flash("No puedes abrir una orden en una mesa marcada como sucia. Márcala como limpia primero.", "warning")
        return redirect(request.referrer or url_for("web.ordenes"))

    existing_order = None if is_takeout else get_active_order_for_mesa(mesa.id)
    if existing_order:
        flash("Esa mesa ya tiene una orden abierta.", "info")
        return redirect(url_for("web.detalle_orden", order_id=existing_order.id))

    order = Orden(
        mesa_id=mesa.id,
        sesion_caja_id=cash_session.id,
        usuario_id=current_user.id,
        nombre_cliente=nombre_cliente or None,
    )
    if not is_takeout:
        mesa.estado = "ocupada"
    db.session.add(order)
    db.session.commit()

    flash(f"Se abrio la orden #{order.id}.", "success")
    return redirect(url_for("web.detalle_orden", order_id=order.id))


@web_bp.get("/ordenes/<int:order_id>")
@feature_required("ordenes")
def detalle_orden(order_id):
    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    if normalize_item_delivery_states(order):
        sync_order(order)
        db.session.commit()

    requested_people = parse_int(request.args.get("personas"), 0)
    split_mode = bool(order.divisiones) or bool_from_form(request.args.get("split"))
    division_count = len(order.divisiones)
    people_count = division_count or requested_people or 2
    people_count = max(2, min(10, people_count))

    can_charge, charge_message = order_can_receive_payment(current_user, order)

    return render_template(
        "order_detail.html",
        page_title=f"Orden #{order.id}",
        order=order,
        products=get_productos(disponibles_only=True),
        split_mode=split_mode,
        split_people_count=people_count,
        split_matrix=build_split_matrix(order, people_count),
        split_labels={
            division.numero_persona: division.etiqueta or ""
            for division in order.divisiones
        },
        can_charge=can_charge,
        charge_message=charge_message,
        can_prepare=current_user.rol in {"dueño", "cocina"},
        can_deliver=current_user.rol in {"dueño", "mesero"},
        division_locked=any(division.pagada for division in order.divisiones),
    )


@web_bp.get("/ordenes/<int:order_id>/ticket")
@login_required
def ticket_orden(order_id):
    if not allow_ticket_access():
        flash("No tienes permiso para ver tickets.", "error")
        return redirect(url_for(default_endpoint_for_user(current_user)))

    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    return render_template("ticket_receipt.html", order=order)


@web_bp.get("/ordenes/<int:order_id>/ticket-cocina")
@login_required
def ticket_cocina(order_id):
    if not allow_ticket_access():
        flash("No tienes permiso para ver tickets.", "error")
        return redirect(url_for(default_endpoint_for_user(current_user)))

    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    return render_template("ticket_kitchen.html", order=order)


@web_bp.post("/ordenes/<int:order_id>/items")
@roles_required("dueño", "cajero", "mesero")
def agregar_item_orden(order_id):
    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    if order.estado != "abierta":
        flash("La orden ya no acepta nuevos items.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    if any(division.pagada for division in order.divisiones):
        flash("No puedes agregar items porque la cuenta dividida ya empezo a cobrarse.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    product_ids = request.form.getlist("product_ids")
    selected_lines = []

    if product_ids:
        seen_product_ids = set()
        for raw_product_id in product_ids:
            product_id = parse_int(raw_product_id)
            if product_id in seen_product_ids:
                continue
            seen_product_ids.add(product_id)

            quantity = parse_int(request.form.get(f"quantity_{product_id}"), 0)
            if quantity <= 0:
                continue

            product = get_producto(product_id)
            if product is None or not product.disponible:
                flash("Uno de los productos elegidos no esta disponible.", "error")
                return redirect(url_for("web.detalle_orden", order_id=order.id))

            selected_lines.append(
                {
                    "product": product,
                    "quantity": quantity,
                    "notes": (request.form.get(f"notes_{product_id}") or "").strip(),
                }
            )
    else:
        product_id = parse_int(request.form.get("product_id"))
        quantity = parse_int(request.form.get("quantity"), 1)
        notes = (request.form.get("notes") or "").strip()

        product = get_producto(product_id)
        if product is None or not product.disponible:
            flash("El producto elegido no esta disponible.", "error")
            return redirect(url_for("web.detalle_orden", order_id=order.id))
        if quantity <= 0:
            flash("La cantidad debe ser mayor que cero.", "error")
            return redirect(url_for("web.detalle_orden", order_id=order.id))

        selected_lines.append({"product": product, "quantity": quantity, "notes": notes})

    if not selected_lines:
        flash("Selecciona al menos un producto para agregar.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    changed, message = reset_divisiones_if_possible(order)
    if message:
        flash(message, "info" if changed else "error")
        if not changed and order.divisiones:
            return redirect(url_for("web.detalle_orden", order_id=order.id))

    for line in selected_lines:
        product = line["product"]
        item = OrdenItem(
            orden=order,
            producto=product,
            cantidad=line["quantity"],
            precio_unitario=product.precio_venta,
            costo_unitario=product.precio_costo,
            notas=line["notes"] or None,
            estado=initial_item_status(product),
        )
        db.session.add(item)

    db.session.flush()
    sync_order(order)
    db.session.commit()

    if len(selected_lines) == 1:
        line = selected_lines[0]
        flash(f"Se agrego {line['product'].nombre} x{line['quantity']}.", "success")
    else:
        total_units = sum(line["quantity"] for line in selected_lines)
        flash(f"Se agregaron {len(selected_lines)} productos ({total_units} unidades).", "success")
    return redirect(url_for("web.detalle_orden", order_id=order.id))


@web_bp.post("/items/<int:item_id>/preparar")
@roles_required("dueño", "cocina")
def preparar_item(item_id):
    item, redirect_response = get_item_or_redirect(item_id)
    if redirect_response:
        return redirect_response

    if not item_can_be_prepared(current_user, item):
        flash("Ese item no puede pasar a listo desde tu rol o estado actual.", "error")
        return redirect(request.referrer or url_for("web.cocina"))

    item.estado = "entregado"
    settle_order(item.orden)
    db.session.commit()
    flash("El item quedo listo y entregado.", "success")
    return redirect(request.referrer or url_for("web.cocina"))


@web_bp.post("/items/<int:item_id>/entregar")
@roles_required("dueño", "mesero")
def entregar_item(item_id):
    item, redirect_response = get_item_or_redirect(item_id)
    if redirect_response:
        return redirect_response

    if not item_can_be_delivered(current_user, item):
        flash("Ese item no esta listo para entregar.", "error")
        return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))

    item.estado = "entregado"
    settle_order(item.orden)
    db.session.commit()

    flash("El item fue marcado como entregado.", "success")
    return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))


@web_bp.post("/items/<int:item_id>/cancelar")
@roles_required("dueño", "cajero", "mesero")
def cancelar_item(item_id):
    item, redirect_response = get_item_or_redirect(item_id)
    if redirect_response:
        return redirect_response

    if item.estado == "cancelado":
        flash("Ese item ya estaba cancelado.", "error")
        return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))
    if item.pagado:
        flash("No puedes cancelar un item que ya fue cobrado.", "error")
        return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))
    if item.estado == "entregado" and current_user.rol != "dueño":
        flash("Solo administracion puede cancelar un item que ya fue entregado.", "error")
        return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))
    if any(division.pagada for division in item.orden.divisiones):
        flash("No puedes cancelar items porque la cuenta dividida ya comenzo a cobrarse.", "error")
        return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))

    changed, message = reset_divisiones_if_possible(item.orden)
    if message:
        flash(message, "info" if changed else "error")
        if not changed and item.orden.divisiones:
            return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))

    item.estado = "cancelado"
    sync_order(item.orden)
    settle_order(item.orden)
    db.session.commit()

    flash("El item fue cancelado.", "success")
    return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))


@web_bp.post("/ordenes/<int:order_id>/dividir")
@roles_required("dueño", "cajero")
def dividir_orden(order_id):
    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    if order.estado != "abierta":
        flash("Solo puedes dividir una orden abierta.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))
    if not order.items_activos:
        flash("Primero agrega items antes de dividir la cuenta.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))
    if any(division.pagada for division in order.divisiones):
        flash("La division ya comenzo a cobrarse y no puede rehacerse.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    people_count = parse_int(request.form.get("people_count"), 2)
    labels, assignments, errors = validate_split_assignment(order, people_count, request.form)
    if errors:
        flash_form_errors(errors)
        return redirect(
            url_for(
                "web.detalle_orden",
                order_id=order.id,
                personas=people_count,
                split=1,
            )
        )

    save_split_configuration(order, people_count, labels, assignments)
    db.session.commit()

    flash("La cuenta quedo dividida por persona.", "success")
    return redirect(
        url_for(
            "web.detalle_orden",
            order_id=order.id,
            personas=people_count,
            split=1,
        )
    )


@web_bp.post("/ordenes/<int:order_id>/dividir/quitar")
@roles_required("dueño", "cajero")
def quitar_division(order_id):
    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    if any(division.pagada for division in order.divisiones):
        flash("No puedes quitar la division porque ya hay personas cobradas.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    clear_divisiones(order)
    db.session.commit()
    flash("La orden volvio al cobro normal.", "success")
    return redirect(url_for("web.detalle_orden", order_id=order.id))


@web_bp.post("/divisiones/<int:division_id>/pagar")
@roles_required("dueño", "cajero")
def pagar_division(division_id):
    division = (
        OrdenDivision.query.options(joinedload(OrdenDivision.orden).joinedload(Orden.mesa))
        .filter_by(id=division_id)
        .first()
    )

    if division is None:
        flash("La division no existe.", "error")
        return redirect(url_for("web.ordenes"))

    method = request.form.get("method")
    if method not in {"efectivo", "tarjeta"}:
        flash("El metodo de pago no es valido.", "error")
        return redirect(url_for("web.detalle_orden", order_id=division.orden_id))

    allowed, message = division_can_receive_payment(current_user, division)
    if not allowed:
        flash(message, "error")
        return redirect(url_for("web.detalle_orden", order_id=division.orden_id))

    payment = Pago(orden=division.orden, metodo=method, monto=division.total)
    division.pagada = True
    db.session.add(payment)
    db.session.flush()
    settle_order(division.orden)
    db.session.commit()

    flash(f"{division.nombre_visible} quedo cobrada.", "success")
    return redirect(url_for("web.detalle_orden", order_id=division.orden_id))


@web_bp.post("/ordenes/<int:order_id>/pagar")
@roles_required("dueño", "cajero")
def pagar_orden(order_id):
    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    if order.divisiones:
        if any(division.pagada for division in order.divisiones):
            flash("Esta orden ya tiene cuenta dividida. Cobra por persona o elimina la division antes del primer pago.", "error")
            return redirect(url_for("web.detalle_orden", order_id=order.id))
        if bool_from_form(request.form.get("clear_split")):
            allowed, message = order_can_receive_payment(current_user, order)
            if not allowed:
                flash(message, "error")
                return redirect(url_for("web.detalle_orden", order_id=order.id))
            clear_divisiones(order)
            db.session.flush()
            order = get_order(order.id)
        else:
            flash("Esta orden tiene cuenta dividida. Quita la division o usa el boton para cobrar normal.", "error")
            return redirect(url_for("web.detalle_orden", order_id=order.id))

    allowed, message = order_can_receive_payment(current_user, order)
    if not allowed:
        flash(message, "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    amount = parse_decimal(request.form.get("amount"))
    method = request.form.get("method")

    if method not in {"efectivo", "tarjeta"}:
        flash("El metodo de pago no es valido.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))
    if amount <= 0:
        flash("El monto debe ser mayor que cero.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))
    if amount > order.saldo_pendiente:
        flash("El monto no puede ser mayor que el saldo pendiente.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    payment = Pago(orden=order, metodo=method, monto=amount)
    db.session.add(payment)
    db.session.flush()
    settle_order(order)
    db.session.commit()

    if order.estado == "pagada":
        flash(f"La orden #{order.id} quedo pagada.", "success")
    else:
        flash("Pago registrado.", "success")
    return redirect(url_for("web.detalle_orden", order_id=order.id))


@web_bp.post("/ordenes/<int:order_id>/cancelar")
@roles_required("dueño", "cajero")
def cancelar_orden(order_id):
    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    if order.total_pagado > 0:
        flash("No puedes cancelar una orden que ya tiene pagos.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))
    if any(item.estado == "entregado" for item in order.items_activos) and current_user.rol != "dueño":
        flash("Solo administracion puede cancelar una orden con items ya entregados.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    order.estado = "cancelada"
    for item in order.items:
        if item.estado != "cancelado":
            item.estado = "cancelado"
    sync_order(order)
    db.session.commit()

    flash(f"La orden #{order.id} fue cancelada.", "success")
    return redirect(url_for("web.ordenes"))


@web_bp.get("/productos")
@feature_required("productos")
def productos():
    search = (request.args.get("q") or "").strip()
    products = get_productos(search=search)
    low_stock = get_low_stock_products(limit=6)
    return render_template(
        "products.html",
        page_title="Productos",
        products=products,
        low_stock_products=low_stock,
        search=search,
    )


@web_bp.get("/productos/nuevo")
@feature_required("productos")
def nuevo_producto():
    return render_template(
        "product_form.html",
        page_title="Nuevo producto",
        product=None,
        categories=get_categorias(),
    )


@web_bp.post("/productos")
@feature_required("productos")
def crear_producto():
    payload, errors = extract_product_payload()
    if errors:
        flash_form_errors(errors)
        return redirect(url_for("web.nuevo_producto"))

    product = Producto(**payload)
    db.session.add(product)
    db.session.commit()

    flash(f"Se creo el producto {product.nombre}.", "success")
    return redirect(url_for("web.productos"))


@web_bp.get("/productos/<int:product_id>/editar")
@feature_required("productos")
def editar_producto(product_id):
    product = get_producto(product_id)
    if product is None:
        flash("El producto no existe.", "error")
        return redirect(url_for("web.productos"))
    return render_template(
        "product_form.html",
        page_title=f"Editar {product.nombre}",
        product=product,
        categories=get_categorias(),
    )


@web_bp.post("/productos/<int:product_id>")
@feature_required("productos")
def actualizar_producto(product_id):
    product = get_producto(product_id)
    if product is None:
        flash("El producto no existe.", "error")
        return redirect(url_for("web.productos"))

    previous_image = product.imagen_url
    payload, errors = extract_product_payload(existing=product)
    if errors:
        flash_form_errors(errors)
        return redirect(url_for("web.editar_producto", product_id=product.id))

    for key, value in payload.items():
        setattr(product, key, value)

    db.session.commit()

    new_image = payload.get("imagen_url")
    if previous_image and previous_image != new_image:
        delete_uploaded_product_image(previous_image)

    flash(f"Se actualizo el producto {product.nombre}.", "success")
    return redirect(url_for("web.productos"))


@web_bp.post("/productos/<int:product_id>/eliminar")
@feature_required("productos")
def eliminar_producto(product_id):
    product = get_producto(product_id)
    if product is None:
        flash("El producto no existe.", "error")
        return redirect(url_for("web.productos"))
    if product.orden_items:
        flash(
            "No puedes borrar un producto que ya aparece en ordenes registradas.",
            "warning",
        )
        return redirect(url_for("web.productos"))
    if product.movimientos_inventario:
        flash(
            "No puedes borrar un producto que ya tiene movimientos de inventario.",
            "warning",
        )
        return redirect(url_for("web.productos"))

    image_to_delete = product.imagen_url
    product_name = product.nombre
    db.session.delete(product)
    db.session.commit()

    delete_uploaded_product_image(image_to_delete)
    flash(f"Se elimino el producto {product_name}.", "success")
    return redirect(url_for("web.productos"))


@web_bp.get("/caja")
@feature_required("caja")
def caja():
    session_open = get_active_cash_session()
    closed_sessions = (
        SesionCaja.query.options(joinedload(SesionCaja.usuario))
        .filter_by(estado="cerrada")
        .order_by(SesionCaja.fecha_cierre.desc())
        .limit(15)
        .all()
    )
    return render_template(
        "cash.html",
        page_title="Caja",
        session_open=session_open,
        expected_cash=session_cash_expected(session_open),
        card_total=session_card_total(session_open),
        sales_total=session_sales_total(session_open),
        recent_movements=recent_cash_movements(
            session_id=session_open.id if session_open else None
        ),
        closed_sessions=closed_sessions,
    )


@web_bp.post("/caja/abrir")
@roles_required("dueño", "cajero")
def abrir_caja():
    if get_active_cash_session() is not None:
        flash("Ya existe una sesion de caja abierta.", "error")
        return redirect(url_for("web.caja"))

    opening_amount = parse_decimal(request.form.get("opening_amount"))
    if opening_amount < 0:
        flash("El monto de apertura no puede ser negativo.", "error")
        return redirect(url_for("web.caja"))

    session_open = SesionCaja(
        usuario_id=current_user.id,
        monto_apertura=opening_amount,
        estado="abierta",
    )
    db.session.add(session_open)
    db.session.commit()

    flash("Caja abierta correctamente.", "success")
    return redirect(url_for("web.caja"))


@web_bp.post("/caja/movimientos")
@roles_required("dueño", "cajero")
def registrar_movimiento_caja():
    session_open = get_active_cash_session()
    if session_open is None:
        flash("No hay caja abierta.", "error")
        return redirect(url_for("web.caja"))

    movement_type = request.form.get("movement_type")
    concept = (request.form.get("concept") or "").strip()
    amount = parse_decimal(request.form.get("amount"))

    if movement_type not in {"ingreso", "egreso"}:
        flash("El tipo de movimiento no es valido.", "error")
        return redirect(url_for("web.caja"))
    if not concept:
        flash("Debes escribir un concepto.", "error")
        return redirect(url_for("web.caja"))
    if amount <= 0:
        flash("El monto debe ser mayor que cero.", "error")
        return redirect(url_for("web.caja"))

    movement = MovimientoCaja(
        sesion_caja_id=session_open.id,
        tipo=movement_type,
        concepto=concept,
        monto=amount,
    )
    db.session.add(movement)
    db.session.commit()

    flash("Movimiento registrado.", "success")
    return redirect(url_for("web.caja"))


@web_bp.post("/caja/cerrar")
@roles_required("dueño", "cajero")
def cerrar_caja():
    session_open = get_active_cash_session()
    if session_open is None:
        flash("No hay una caja abierta para cerrar.", "error")
        return redirect(url_for("web.caja"))

    closing_amount = parse_decimal(request.form.get("closing_amount"))
    session_open.monto_cierre_real = closing_amount
    session_open.fecha_cierre = datetime.utcnow()
    session_open.estado = "cerrada"
    db.session.commit()

    flash("Caja cerrada correctamente.", "success")
    return redirect(url_for("web.caja"))


@web_bp.get("/reportes")
@feature_required("reportes")
def reportes():
    start_date, end_date = parse_report_range()
    report = get_report_snapshot(start_date, end_date)
    return render_template(
        "reports.html",
        page_title="Reportes",
        report=report,
        start_date=start_date,
        end_date=end_date,
    )


@web_bp.get("/reportes/export/pdf")
@feature_required("reportes")
def exportar_reporte_pdf():
    start_date, end_date = parse_report_range()
    report = get_report_snapshot(start_date, end_date)
    return build_pdf_response(
        f"reporte_{start_date.isoformat()}_{end_date.isoformat()}.pdf",
        start_date,
        end_date,
        report,
    )


@web_bp.get("/reportes/export/<string:kind>")
@feature_required("reportes")
def exportar_reporte(kind):
    start_date, end_date = parse_report_range()

    if kind == "ventas":
        payments = get_payments_for_range(start_date, end_date)
        return build_csv_response(
            f"ventas_{start_date.isoformat()}_{end_date.isoformat()}.csv",
            ["Fecha", "Orden", "Mesa", "Cliente", "Metodo", "Monto"],
            [
                [
                    payment.created_at.strftime("%Y-%m-%d %H:%M"),
                    f"#{payment.orden_id}",
                    payment.orden.mesa.etiqueta
                    if payment.orden and payment.orden.mesa
                    else "-",
                    payment.orden.nombre_cliente if payment.orden else "",
                    payment.metodo,
                    f"{payment.monto}",
                ]
                for payment in payments
            ],
        )

    if kind == "productos":
        products = get_productos()
        return build_csv_response(
            "productos_catalogo.csv",
            [
                "Producto",
                "Categoria",
                "Precio venta",
                "Precio costo",
                "Stock",
                "Disponible",
                "Imagen",
            ],
            [
                [
                    product.nombre,
                    product.categoria.nombre if product.categoria else "",
                    f"{product.precio_venta}",
                    f"{product.precio_costo}",
                    product.stock_actual,
                    "Si" if product.disponible else "No",
                    product.imagen_url or "",
                ]
                for product in products
            ],
        )

    if kind == "inventario":
        movements = get_inventory_for_range(start_date, end_date)
        return build_csv_response(
            f"inventario_{start_date.isoformat()}_{end_date.isoformat()}.csv",
            ["Fecha", "Producto", "Tipo", "Unidades", "Usuario", "Notas"],
            [
                [
                    movement.created_at.strftime("%Y-%m-%d %H:%M"),
                    movement.producto.nombre if movement.producto else "",
                    movement.tipo,
                    movement.cantidad_unidades,
                    movement.usuario.nombre_completo if movement.usuario else "",
                    movement.notas or "",
                ]
                for movement in movements
            ],
        )

    if kind == "ordenes":
        orders = get_orders_for_range(start_date, end_date)
        return build_csv_response(
            f"ordenes_{start_date.isoformat()}_{end_date.isoformat()}.csv",
            ["Orden", "Mesa", "Cliente", "Mesero", "Estado", "Total", "Pagado", "Creada"],
            [
                [
                    f"#{order.id}",
                    order.mesa.etiqueta if order.mesa else "",
                    order.nombre_cliente or "",
                    order.usuario.nombre_completo if order.usuario else "",
                    order.estado,
                    f"{order.total}",
                    f"{order.total_pagado}",
                    order.created_at.strftime("%Y-%m-%d %H:%M"),
                ]
                for order in orders
            ],
        )

    flash("Ese reporte no existe.", "error")
    return redirect(url_for("web.reportes"))


@web_bp.get("/cocina")
@feature_required("cocina")
def cocina():
    return render_template(
        "kitchen.html",
        page_title="Cocina",
        pending_items=get_pending_kitchen_items(),
        prepare_url_template=url_for("web.preparar_item", item_id=0).replace("0", "__ID__"),
    )


@web_bp.get("/inventario")
@feature_required("inventario")
def inventario():
    return render_template(
        "inventory.html",
        page_title="Inventario",
        products=get_inventory_products(),
        movements=recent_inventory_movements(),
        low_stock_products=get_low_stock_products(limit=8),
    )


@web_bp.get("/inventario/nuevo")
@feature_required("inventario")
def nuevo_movimiento_inventario():
    return render_template(
        "inventory_form.html",
        page_title="Nuevo movimiento de inventario",
        products=get_inventory_products(),
    )


@web_bp.post("/inventario")
@feature_required("inventario")
def registrar_movimiento_inventario():
    product_id = parse_int(request.form.get("product_id"))
    movement_type = request.form.get("movement_type")
    packages = request.form.get("packages")
    units = parse_int(request.form.get("units"))
    unit_price = parse_decimal(request.form.get("unit_price"), default=None)
    notes = (request.form.get("notes") or "").strip()

    product = db.session.get(Producto, product_id)
    if product is None:
        flash("El producto seleccionado no existe.", "error")
        return redirect(url_for("web.nuevo_movimiento_inventario"))
    if not product.controla_stock:
        flash(
            "Ese producto no maneja inventario directo. Los productos de cocina se controlan por insumos.",
            "warning",
        )
        return redirect(url_for("web.nuevo_movimiento_inventario"))
    if movement_type not in {"compra", "venta", "ajuste"}:
        flash("El tipo de movimiento no es valido.", "error")
        return redirect(url_for("web.nuevo_movimiento_inventario"))
    if units == 0:
        flash("La cantidad de unidades no puede ser cero.", "error")
        return redirect(url_for("web.nuevo_movimiento_inventario"))

    if movement_type == "compra":
        product.stock_actual += abs(units)
        stored_units = abs(units)
    elif movement_type == "venta":
        product.stock_actual -= abs(units)
        stored_units = abs(units)
    else:
        product.stock_actual += units
        stored_units = units

    movement = MovimientoInventario(
        producto_id=product.id,
        tipo=movement_type,
        cantidad_paquetes=parse_int(packages, default=None) if packages else None,
        cantidad_unidades=stored_units,
        precio_unitario=unit_price,
        notas=notes or None,
        usuario_id=current_user.id,
    )
    db.session.add(movement)
    db.session.commit()

    flash("Movimiento de inventario registrado.", "success")
    return redirect(url_for("web.inventario"))


@api_bp.get("/health")
def api_health():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "conectada"})
    except Exception as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "database": "sin conexion",
                    "detail": str(exc),
                }
            ),
            503,
        )


@api_bp.get("/me")
@login_required
def api_me():
    return jsonify(current_user.to_dict())


@api_bp.get("/dashboard")
@login_required
def api_dashboard():
    try:
        return jsonify(get_dashboard_metrics())
    except SQLAlchemyError as exc:
        return database_error_response(exc)


@api_bp.get("/cocina/pending")
@login_required
def api_cocina_pending():
    if not user_can(current_user, "cocina"):
        return api_permission_denied()

    try:
        items = get_pending_kitchen_items()
        return jsonify(
            {
                "count": len(items),
                "updated_at": datetime.utcnow().isoformat(),
                "items": [serialize_kitchen_item(item) for item in items],
            }
        )
    except SQLAlchemyError as exc:
        return database_error_response(exc)


@api_bp.get("/mesas")
@login_required
def api_mesas():
    try:
        zonas, active_orders = grouped_tables()
        payload = []
        for zona in zonas:
            for mesa in zona.mesas:
                active_order = active_orders.get(mesa.id)
                data = mesa.to_dict()
                data["orden_abierta_id"] = active_order.id if active_order else None
                data["orden_total"] = float(active_order.total) if active_order else 0
                payload.append(data)
        return jsonify(payload)
    except SQLAlchemyError as exc:
        return database_error_response(exc)


@api_bp.get("/productos")
@login_required
def api_productos():
    try:
        return jsonify([product.to_dict() for product in get_productos()])
    except SQLAlchemyError as exc:
        return database_error_response(exc)


@api_bp.get("/ordenes/abiertas")
@login_required
def api_ordenes_abiertas():
    try:
        orders = get_orders_for_listing(status="abierta", date_value=None)
        return jsonify([order.to_dict() for order in orders])
    except SQLAlchemyError as exc:
        return database_error_response(exc)


@api_bp.get("/ordenes/<int:order_id>/estado")
@login_required
def api_orden_estado(order_id):
    if not user_can(current_user, "ordenes"):
        return api_permission_denied()

    try:
        order = get_order(order_id)
        if order is None:
            return jsonify({"status": "error", "message": "La orden no existe."}), 404

        if normalize_item_delivery_states(order):
            sync_order(order)
            db.session.commit()

        requested_people = parse_int(request.args.get("personas"), 0)
        division_count = len(order.divisiones)
        people_count = division_count or requested_people or 2
        people_count = max(2, min(10, people_count))
        can_charge, charge_message = order_can_receive_payment(current_user, order)
        division_locked = any(division.pagada for division in order.divisiones)
        context = {
            "order": order,
            "can_charge": can_charge,
            "charge_message": charge_message,
            "can_prepare": current_user.rol in {"dueño", "cocina"},
            "can_deliver": current_user.rol in {"dueño", "mesero"},
            "division_locked": division_locked,
            "owner_user": current_user.rol == "dueño",
            "split_people_count": people_count,
            "split_matrix": build_split_matrix(order, people_count),
            "split_labels": {
                division.numero_persona: division.etiqueta or ""
                for division in order.divisiones
            },
        }

        return jsonify(
            {
                "status": "ok",
                "delivered_count": len(order.items_entregados),
                "items_html": render_template("partials/order_items_list.html", **context),
                "payment_html": render_template("partials/order_payment_panel.html", **context),
                "division_cards_html": render_template(
                    "partials/order_division_cards.html", **context
                ),
                "charge_status_html": render_template(
                    "partials/order_charge_status.html", **context
                ),
            }
        )
    except SQLAlchemyError as exc:
        return database_error_response(exc)
