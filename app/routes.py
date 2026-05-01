import csv
import re
from datetime import datetime
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
    AuditLog,
    Categoria,
    ListaEspera,
    Mesa,
    MovimientoCaja,
    MovimientoInventario,
    Orden,
    OrdenDivision,
    OrdenDivisionItem,
    OrdenItem,
    Pago,
    Producto,
    Rol,
    SesionCaja,
    Usuario,
    Zona,
)
from .services import (
    ADMIN_ROLE_CODE,
    add_or_increment_order_item,
    audit_event,
    available_stock_units,
    bool_from_form,
    build_split_matrix,
    clear_divisiones,
    consolidate_order_items,
    default_endpoint_for_user,
    default_theme,
    division_can_receive_payment,
    feature_required,
    feature_definitions,
    format_local_datetime,
    get_system_preferences,
    get_waitlist_entries,
    permission_definitions,
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
    get_role,
    get_roles,
    get_producto,
    get_productos,
    get_report_snapshot,
    get_inventory_for_range,
    get_user,
    get_user_by_nickname,
    get_zona,
    get_zonas,
    grouped_tables,
    item_can_be_delivered,
    item_can_be_prepared,
    local_now,
    local_today,
    normalize_item_delivery_states,
    money as normalize_money,
    order_can_receive_payment,
    parse_date_value,
    parse_decimal,
    parse_int,
    recent_cash_movements,
    recent_inventory_movements,
    reset_divisiones_if_possible,
    save_system_preferences,
    save_split_configuration,
    serialize_kitchen_item,
    session_card_total,
    session_cash_expected,
    session_sales_total,
    settle_order,
    stock_request_errors,
    order_stock_errors,
    sync_order,
    LOW_STOCK_THRESHOLD,
    system_preference_choices,
    theme_choices,
    user_can,
    valid_timezone_name,
    valid_role_code,
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
    return parse_date_value(raw_value, local_today())


def parse_report_range():
    today = local_today()
    default_start = today.replace(day=1)
    start_date = parse_date_value(request.args.get("desde"), default_start)
    end_date = parse_date_value(request.args.get("hasta"), today)
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def ticket_payment_key(order_id, division_id=None):
    return f"division:{division_id}" if division_id else str(order_id)


def remember_ticket_payment(order_id, amount, method, division_id=None):
    received = parse_decimal(request.form.get("tendered_amount"), amount)
    if method != "efectivo" or received <= 0:
        received = amount
    if received < amount:
        received = amount

    change = received - amount if method == "efectivo" else parse_decimal("0")
    ticket_payments = dict(session.get("ticket_payments", {}))
    ticket_payments[ticket_payment_key(order_id, division_id)] = {
        "method": method,
        "amount": f"{amount:.2f}",
        "received": f"{received:.2f}",
        "change": f"{change:.2f}",
    }
    session["ticket_payments"] = ticket_payments


def ticket_payment_context(order, total=None, division_id=None):
    ticket_payments = session.get("ticket_payments", {})
    stored = ticket_payments.get(ticket_payment_key(order.id, division_id), {})
    latest_payment = order.pagos[-1] if order.pagos else None
    method = stored.get("method") or (latest_payment.metodo if latest_payment else "")
    fallback_total = total if total is not None else order.total
    amount = parse_decimal(
        stored.get("amount"),
        total if total is not None else (latest_payment.monto if latest_payment else fallback_total),
    )
    received = parse_decimal(
        stored.get("received"),
        fallback_total if division_id is not None else (order.total_pagado if latest_payment else parse_decimal("0")),
    )
    change = parse_decimal(stored.get("change"))

    return {
        "method": method,
        "amount": amount,
        "received": received,
        "change": change,
    }


def ticket_lines_for_order(order):
    return [
        {
            "quantity": item.cantidad,
            "description": item.producto.nombre if item.producto else "-",
            "notes": item.notas,
            "subtotal": item.subtotal,
        }
        for item in order.items_activos
    ]


def ticket_lines_for_division(division):
    lines = []
    for division_item in division.items:
        order_item = division_item.orden_item
        product = order_item.producto if order_item else None
        lines.append(
            {
                "quantity": division_item.cantidad,
                "description": product.nombre if product else "-",
                "notes": order_item.notas if order_item else "",
                "subtotal": division_item.subtotal,
            }
        )
    return lines


def flash_low_stock_for_order(order):
    seen_product_ids = set()
    for item in order.items_activos:
        product = item.producto
        if (
            not product
            or not product.controla_stock
            or product.id in seen_product_ids
            or product.stock_actual > LOW_STOCK_THRESHOLD
        ):
            continue
        seen_product_ids.add(product.id)
        flash(
            f"Stock bajo: {product.nombre} queda con {product.stock_actual} unidades.",
            "warning",
        )


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


def save_brand_logo(file_storage):
    if file_storage is None or not file_storage.filename:
        return None, None

    filename = secure_filename(file_storage.filename)
    extension = Path(filename).suffix.lower()

    if extension not in IMAGE_EXTENSIONS:
        return None, "El logo debe ser JPG, PNG, WEBP o GIF."

    upload_dir = current_app.config["BRANDING_UPLOAD_DIR"]
    upload_dir.mkdir(parents=True, exist_ok=True)

    generated_name = f"{datetime.utcnow():%Y%m%d%H%M%S}_{uuid4().hex[:10]}{extension}"
    destination = upload_dir / generated_name
    file_storage.save(destination)
    return f"/static/uploads/branding/{generated_name}", None


def delete_uploaded_brand_logo(image_url):
    if not image_url or not image_url.startswith("/static/uploads/branding/"):
        return

    relative_name = image_url.replace("/static/uploads/branding/", "", 1)
    file_path = current_app.config["BRANDING_UPLOAD_DIR"] / relative_name
    if file_path.exists():
        file_path.unlink(missing_ok=True)


def extract_product_payload(existing=None):
    image_url = (request.form.get("image_url") or "").strip()
    uploaded_image, upload_error = save_product_image(request.files.get("image_file"))

    name = (request.form.get("name") or "").strip()
    category_id = parse_int(request.form.get("category_id"))
    package_cost = parse_decimal(request.form.get("cost_price"))
    sale_price = parse_decimal(request.form.get("sale_price"))
    purchase_unit = (request.form.get("purchase_unit") or "unidad").strip() or "unidad"
    units_per_package = max(parse_int(request.form.get("units_per_package"), 1), 1)
    stock_packages = parse_int(request.form.get("stock_packages"), 0)
    stock_units = parse_int(request.form.get("current_stock"), 0)
    current_stock = (stock_packages * units_per_package) + stock_units
    cost_price = (
        normalize_money(package_cost / units_per_package)
        if package_cost > 0
        else parse_decimal("0")
    )
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
    if stock_packages < 0 or stock_units < 0 or current_stock < 0:
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
    preferences = get_system_preferences()
    business_name = preferences["business_name"]
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

    generated_at = format_local_datetime(local_now(), "datetime", preferences)

    story = [
        Paragraph(business_name, title_style),
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
        canvas.drawString(
            doc.leftMargin,
            page_height - 20,
            f"{business_name.upper()} - REPORTE EJECUTIVO",
        )

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
        canvas.drawRightString(page_width - doc.rightMargin, 11, f"Sistema {business_name}")

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
        user_can(current_user, "ordenes.ticket")
        or user_can(current_user, "cocina.view")
        or user_can(current_user, "caja")
    )


def normalize_role_code(value):
    cleaned = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
    return cleaned.strip("_")


def local_datetime_label(value):
    if not value:
        return ""
    return format_local_datetime(value, "datetime")


@web_bp.get("/")
@login_required
def home():
    return redirect(url_for(default_endpoint_for_user(current_user)))


@web_bp.get("/login")
def login():
    reauth = current_user.is_authenticated and not login_fresh()
    if current_user.is_authenticated and not reauth:
        default_endpoint = default_endpoint_for_user(current_user)
        if default_endpoint != "web.login":
            return redirect(url_for(default_endpoint))
        logout_user()
        flash("Tu rol no tiene permisos activos. Inicia sesion con un administrador.", "error")
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

    default_endpoint = default_endpoint_for_user(user)
    if default_endpoint == "web.login":
        logout_user()
        flash("Ese usuario no tiene permisos activos. Revisa su rol desde un administrador.", "error")
        return redirect(url_for("web.login"))

    if user.must_change_password and not reauth:
        flash("Actualiza tu contrasena para continuar.", "info")
        return redirect(url_for("web.mi_seguridad"))

    return redirect(next_url_or_default(default_endpoint))


@web_bp.get("/perfil/seguridad")
@login_required
def mi_seguridad():
    return render_template("profile_security.html", page_title="Mi seguridad")


@web_bp.post("/perfil/seguridad")
@login_required
def actualizar_mi_password():
    current_password = request.form.get("current_password") or ""
    new_password = request.form.get("new_password") or ""
    confirm_password = request.form.get("confirm_password") or ""

    errors = []
    if not current_user.check_password(current_password):
        errors.append("Tu contrasena actual no coincide.")
    if len(new_password) < 6:
        errors.append("La nueva contrasena debe tener al menos 6 caracteres.")
    if new_password != confirm_password:
        errors.append("La confirmacion no coincide.")

    if errors:
        flash_form_errors(errors)
        return redirect(url_for("web.mi_seguridad"))

    current_user.set_password(new_password)
    current_user.must_change_password = False
    audit_event("cambiar_password", "usuario", current_user.id, "El usuario cambio su propia contrasena.")
    db.session.commit()

    flash("Tu contrasena fue actualizada.", "success")
    return redirect(url_for(default_endpoint_for_user(current_user)))


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


@web_bp.get("/preferencias")
@fresh_login_required
@feature_required("preferencias.view")
def preferencias():
    return render_template(
        "preferences.html",
        page_title="Preferencias",
        preferences=get_system_preferences(),
        choices=system_preference_choices(),
    )


@web_bp.post("/preferencias")
@fresh_login_required
@feature_required("preferencias.edit")
def actualizar_preferencias():
    current_preferences = get_system_preferences()
    uploaded_logo, upload_error = save_brand_logo(request.files.get("logo_file"))
    remove_logo = bool_from_form(request.form.get("remove_logo"))
    logo_url = (request.form.get("business_logo_url") or "").strip()

    payload = {
        "business_name": (request.form.get("business_name") or "").strip(),
        "business_tagline": (request.form.get("business_tagline") or "").strip(),
        "business_logo_url": current_preferences.get("business_logo_url", ""),
        "timezone": (request.form.get("timezone") or "").strip(),
        "date_format": (request.form.get("date_format") or "").strip(),
        "time_format": (request.form.get("time_format") or "").strip(),
        "sidebar_clock": (request.form.get("sidebar_clock") or "").strip(),
        "default_theme": (request.form.get("default_theme") or "").strip(),
        "ticket_footer": (request.form.get("ticket_footer") or "").strip(),
    }

    if logo_url:
        payload["business_logo_url"] = logo_url
    if uploaded_logo:
        payload["business_logo_url"] = uploaded_logo
    if remove_logo:
        payload["business_logo_url"] = ""

    choices = system_preference_choices()
    valid_date_formats = {choice["value"] for choice in choices["date_formats"]}
    valid_time_formats = {choice["value"] for choice in choices["time_formats"]}
    valid_sidebar_clocks = {choice["value"] for choice in choices["sidebar_clocks"]}
    valid_themes = {choice["value"] for choice in choices["themes"]}

    errors = []
    if upload_error:
        errors.append(upload_error)
    if not payload["business_name"]:
        errors.append("El nombre del negocio es obligatorio.")
    if logo_url and not valid_image_reference(logo_url):
        errors.append("La URL del logo debe iniciar con http://, https:// o /static/.")
    if payload["timezone"] and not valid_timezone_name(payload["timezone"]):
        errors.append("La zona horaria seleccionada no es valida.")
    if payload["date_format"] not in valid_date_formats:
        errors.append("Selecciona un formato de fecha valido.")
    if payload["time_format"] not in valid_time_formats:
        errors.append("Selecciona un formato de hora valido.")
    if payload["sidebar_clock"] not in valid_sidebar_clocks:
        errors.append("Selecciona que debe mostrar el reloj del menu.")
    if payload["default_theme"] not in valid_themes:
        errors.append("Selecciona un tema inicial valido.")

    if errors:
        if uploaded_logo:
            delete_uploaded_brand_logo(uploaded_logo)
        flash_form_errors(errors)
        return redirect(url_for("web.preferencias"))

    if remove_logo and uploaded_logo:
        delete_uploaded_brand_logo(uploaded_logo)

    old_logo = current_preferences.get("business_logo_url")
    saved_preferences = save_system_preferences(payload)
    if old_logo and old_logo != saved_preferences["business_logo_url"] and old_logo.startswith(
        "/static/uploads/branding/"
    ):
        delete_uploaded_brand_logo(old_logo)

    session["theme"] = saved_preferences["default_theme"]
    audit_event(
        "actualizar",
        "preferencias",
        "sistema",
        "Se actualizaron las preferencias generales del sistema.",
        {"preferencias": saved_preferences},
    )
    db.session.commit()

    flash("Preferencias guardadas correctamente.", "success")
    return redirect(url_for("web.preferencias"))


@web_bp.get("/dashboard")
@feature_required("dashboard.view")
def dashboard():
    snapshot = get_dashboard_snapshot()
    return render_template(
        "dashboard.html",
        page_title="Inicio",
        dashboard_date=local_today().isoformat(),
        **snapshot,
    )


@web_bp.get("/zonas")
@feature_required("zonas.view")
def zonas():
    return render_template(
        "zones.html",
        page_title="Zonas",
        zones=get_zonas(),
    )


@web_bp.get("/zonas/nueva")
@feature_required("zonas.create")
def nueva_zona():
    return render_template("zone_form.html", page_title="Nueva zona", zone=None)


@web_bp.get("/zonas/<int:zone_id>/editar")
@feature_required("zonas.edit")
def editar_zona(zone_id):
    zone = get_zona(zone_id)
    if zone is None:
        flash("La zona no existe.", "error")
        return redirect(url_for("web.zonas"))
    return render_template("zone_form.html", page_title="Editar zona", zone=zone)


@web_bp.post("/zonas")
@feature_required("zonas.create")
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
    audit_event("crear", "zona", None, f"Se creo la zona {nombre}.")
    db.session.commit()
    flash(f"Se creó la zona {zone.nombre}.", "success")
    return redirect(url_for("web.zonas"))


@web_bp.post("/zonas/<int:zone_id>")
@feature_required("zonas.edit")
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
    audit_event("actualizar", "zona", zone.id, f"Se actualizo la zona {zone.nombre}.")
    db.session.commit()
    flash(f"Se actualizó la zona {zone.nombre}.", "success")
    return redirect(url_for("web.zonas"))


@web_bp.post("/zonas/<int:zone_id>/eliminar")
@feature_required("zonas.delete")
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
    audit_event("eliminar", "zona", zone_id, f"Se elimino la zona {zone_name}.")
    db.session.commit()
    flash(f"La zona {zone_name} fue eliminada.", "success")
    return redirect(url_for("web.zonas"))


@web_bp.get("/categorias")
@feature_required("categorias.view")
def categorias():
    return render_template(
        "categories.html",
        page_title="Categorias",
        categories=get_categorias(),
    )


@web_bp.get("/categorias/nueva")
@feature_required("categorias.create")
def nueva_categoria():
    return render_template(
        "category_form.html",
        page_title="Nueva categoria",
        category=None,
    )


@web_bp.get("/categorias/<int:category_id>/editar")
@feature_required("categorias.edit")
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
@feature_required("categorias.create")
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
    audit_event("crear", "categoria", None, f"Se creo la categoria {nombre}.")
    db.session.commit()
    flash(f"Se creo la categoria {category.nombre}.", "success")
    return redirect(url_for("web.categorias"))


@web_bp.post("/categorias/<int:category_id>")
@feature_required("categorias.edit")
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
    audit_event("actualizar", "categoria", category.id, f"Se actualizo la categoria {category.nombre}.")
    db.session.commit()
    flash(f"Se actualizo la categoria {category.nombre}.", "success")
    return redirect(url_for("web.categorias"))


@web_bp.post("/categorias/<int:category_id>/eliminar")
@feature_required("categorias.delete")
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
    audit_event("eliminar", "categoria", category_id, f"Se elimino la categoria {category_name}.")
    db.session.commit()
    flash(f"La categoria {category_name} fue eliminada.", "success")
    return redirect(url_for("web.categorias"))


@web_bp.get("/usuarios")
@fresh_login_required
@feature_required("usuarios.view")
def usuarios():
    users = Usuario.query.order_by(Usuario.activo.desc(), Usuario.nombre.asc()).all()
    return render_template(
        "users.html",
        page_title="Usuarios",
        users=users,
        roles=get_roles(),
    )


@web_bp.get("/roles")
@fresh_login_required
@feature_required("roles.view")
def roles():
    roles_list = get_roles()
    users_by_role = {
        role.codigo: Usuario.query.filter_by(rol=role.codigo).count()
        for role in roles_list
    }
    return render_template(
        "roles.html",
        page_title="Roles",
        roles=roles_list,
        features=permission_definitions(),
        users_by_role=users_by_role,
    )


@web_bp.get("/roles/nuevo")
@fresh_login_required
@feature_required("roles.create")
def nuevo_rol():
    role = Rol(codigo="", nombre="", descripcion="")
    role.permisos = set()
    return render_template(
        "role_form.html",
        page_title="Nuevo rol",
        role=role,
        features=permission_definitions(),
        is_new_role=True,
    )


@web_bp.post("/roles")
@fresh_login_required
@feature_required("roles.create")
def crear_rol():
    nombre = (request.form.get("nombre") or "").strip()
    codigo = normalize_role_code(request.form.get("codigo") or nombre)
    descripcion = (request.form.get("descripcion") or "").strip()
    selected_permissions = set(request.form.getlist("permisos"))
    valid_permissions = {permission["key"] for permission in permission_definitions()}

    errors = []
    if not nombre:
        errors.append("El rol necesita un nombre.")
    if not codigo:
        errors.append("El rol necesita un codigo valido.")
    if len(codigo) > 30:
        errors.append("El codigo del rol no puede superar 30 caracteres.")
    if get_role(codigo):
        errors.append("Ya existe un rol con ese codigo.")
    if not selected_permissions:
        errors.append("Selecciona al menos un permiso para este rol.")
    if selected_permissions - valid_permissions:
        errors.append("Hay permisos no validos en el formulario.")

    if errors:
        flash_form_errors(errors)
        return redirect(url_for("web.nuevo_rol"))

    role = Rol(codigo=codigo, nombre=nombre, descripcion=descripcion or None)
    role.permisos = selected_permissions
    db.session.add(role)
    audit_event(
        "crear",
        "rol",
        codigo,
        f"Se creo el rol {nombre}.",
        {"permisos": sorted(selected_permissions)},
    )
    db.session.commit()

    flash(f"Se creo el rol {role.nombre}.", "success")
    return redirect(url_for("web.roles"))


@web_bp.get("/roles/<role_code>/editar")
@fresh_login_required
@feature_required("roles.edit")
def editar_rol(role_code):
    role = get_role(role_code)
    if role is None:
        flash("El rol no existe.", "error")
        return redirect(url_for("web.roles"))
    return render_template(
        "role_form.html",
        page_title=f"Editar rol {role.nombre}",
        role=role,
        features=permission_definitions(),
        is_new_role=False,
    )


@web_bp.post("/roles/<role_code>")
@fresh_login_required
@feature_required("roles.edit")
def actualizar_rol(role_code):
    role = db.session.get(Rol, role_code)
    if role is None:
        flash("El rol no existe.", "error")
        return redirect(url_for("web.roles"))

    nombre = (request.form.get("nombre") or "").strip()
    descripcion = (request.form.get("descripcion") or "").strip()
    selected_permissions = set(request.form.getlist("permisos"))
    valid_permissions = {permission["key"] for permission in permission_definitions()}

    errors = []
    if not nombre:
        errors.append("El rol necesita un nombre.")
    if not selected_permissions:
        errors.append("Selecciona al menos un permiso para este rol.")
    if selected_permissions - valid_permissions:
        errors.append("Hay permisos no validos en el formulario.")
    if current_user.rol == role.codigo and "usuarios.view" not in selected_permissions:
        errors.append("No puedes quitarle a tu propio rol el permiso de Usuarios y roles.")

    if errors:
        flash_form_errors(errors)
        return redirect(url_for("web.editar_rol", role_code=role.codigo))

    role.nombre = nombre
    role.descripcion = descripcion or None
    role.permisos = selected_permissions
    audit_event(
        "actualizar",
        "rol",
        role.codigo,
        f"Se actualizaron permisos de {role.nombre}.",
        {"permisos": sorted(selected_permissions)},
    )
    db.session.commit()

    flash(f"Se actualizaron los permisos de {role.nombre}.", "success")
    return redirect(url_for("web.roles"))


@web_bp.post("/roles/<role_code>/eliminar")
@fresh_login_required
@feature_required("roles.delete")
def eliminar_rol(role_code):
    role = db.session.get(Rol, role_code)
    if role is None:
        flash("El rol no existe.", "error")
        return redirect(url_for("web.roles"))
    if role.codigo == ADMIN_ROLE_CODE:
        flash("No puedes eliminar el rol base de administrador.", "warning")
        return redirect(url_for("web.roles"))
    if current_user.rol == role.codigo:
        flash("No puedes eliminar tu propio rol.", "warning")
        return redirect(url_for("web.roles"))
    if Usuario.query.filter_by(rol=role.codigo).first():
        flash("No puedes eliminar un rol que todavia tiene usuarios asignados.", "warning")
        return redirect(url_for("web.roles"))

    role_name = role.nombre
    db.session.delete(role)
    audit_event("eliminar", "rol", role_code, f"Se elimino el rol {role_name}.")
    db.session.commit()

    flash(f"Se elimino el rol {role_name}.", "success")
    return redirect(url_for("web.roles"))


@web_bp.get("/usuarios/nuevo")
@fresh_login_required
@feature_required("usuarios.create")
def nuevo_usuario():
    return render_template(
        "user_form.html",
        page_title="Nuevo usuario",
        user=None,
        roles=get_roles(),
    )


@web_bp.get("/usuarios/<int:user_id>/editar")
@fresh_login_required
@feature_required("usuarios.edit")
def editar_usuario(user_id):
    user = get_user(user_id)
    if user is None:
        flash("El usuario no existe.", "error")
        return redirect(url_for("web.usuarios"))
    return render_template(
        "user_form.html",
        page_title="Editar usuario",
        user=user,
        roles=get_roles(),
    )


@web_bp.post("/usuarios")
@fresh_login_required
@feature_required("usuarios.create")
def crear_usuario():
    nickname = (request.form.get("nickname") or "").strip()
    nombre = (request.form.get("nombre") or "").strip()
    apellido = (request.form.get("apellido") or "").strip()
    rol = request.form.get("rol")
    password = request.form.get("password") or ""
    activo = bool_from_form(request.form.get("activo"))
    must_change_password = bool_from_form(request.form.get("must_change_password"))

    errors = []
    if not nickname:
        errors.append("El usuario necesita un nickname.")
    if not nombre:
        errors.append("El usuario necesita un nombre.")
    if not apellido:
        errors.append("El usuario necesita un apellido.")
    if not valid_role_code(rol):
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
        must_change_password=must_change_password,
    )
    user.set_password(password)
    db.session.add(user)
    audit_event(
        "crear",
        "usuario",
        nickname,
        f"Se creo el usuario {nombre} {apellido}.",
        {"rol": rol, "activo": activo, "must_change_password": must_change_password},
    )
    db.session.commit()

    flash(f"Se creo el usuario {user.nombre_completo}.", "success")
    return redirect(url_for("web.usuarios"))


@web_bp.post("/usuarios/<int:user_id>")
@fresh_login_required
@feature_required("usuarios.edit")
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
    must_change_password = bool_from_form(request.form.get("must_change_password"))

    errors = []
    other = Usuario.query.filter(Usuario.nickname == nickname, Usuario.id != user.id).first()
    if other:
        errors.append("Ese nickname ya le pertenece a otra persona.")
    if not valid_role_code(rol):
        errors.append("El rol seleccionado no es valido.")
    if current_user.id == user.id and not activo:
        errors.append("No puedes desactivar tu propia cuenta.")
    selected_role = get_role(rol)
    if current_user.id == user.id and (
        selected_role is None or "usuarios.view" not in selected_role.permisos
    ):
        errors.append("No puedes quitarte a ti mismo el acceso a usuarios y roles.")
    if password and current_user.id != user.id and not user_can(
        current_user, "usuarios.reset_password"
    ):
        errors.append("No tienes permiso para resetear contrasenas de otros usuarios.")

    if errors:
        flash_form_errors(errors)
        return redirect(url_for("web.editar_usuario", user_id=user.id))

    user.nickname = nickname or user.nickname
    user.nombre = nombre or user.nombre
    user.apellido = apellido or user.apellido
    user.rol = rol
    user.activo = activo
    user.must_change_password = must_change_password

    if password:
        if len(password) < 6:
            flash("La nueva contrasena debe tener al menos 6 caracteres.", "error")
            return redirect(url_for("web.editar_usuario", user_id=user.id))
        user.set_password(password)
        user.must_change_password = True

    audit_event(
        "actualizar",
        "usuario",
        user.id,
        f"Se actualizo el usuario {user.nombre_completo}.",
        {
            "rol": user.rol,
            "activo": user.activo,
            "password_reset": bool(password),
            "must_change_password": user.must_change_password,
        },
    )
    db.session.commit()
    flash(f"Se actualizo el usuario {user.nombre_completo}.", "success")
    return redirect(url_for("web.usuarios"))


@web_bp.post("/usuarios/<int:user_id>/eliminar")
@fresh_login_required
@feature_required("usuarios.delete")
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
    audit_event("eliminar", "usuario", user_id, f"Se elimino el usuario {user_name}.")
    db.session.commit()
    flash(f"Se eliminó el usuario {user_name}.", "success")
    return redirect(url_for("web.usuarios"))


@web_bp.get("/auditoria")
@fresh_login_required
@feature_required("auditoria.view")
def auditoria():
    logs = (
        AuditLog.query.options(joinedload(AuditLog.usuario))
        .order_by(AuditLog.created_at.desc())
        .limit(200)
        .all()
    )
    return render_template("audit_logs.html", page_title="Auditoria", logs=logs)


@web_bp.get("/mesas")
@feature_required("mesas.view")
def mesas():
    zonas, active_orders = grouped_tables()
    waitlist_entries = get_waitlist_entries()
    zone_cards = []
    table_summary = {
        "total": 0,
        "available": 0,
        "occupied": 0,
        "active_accounts": 0,
        "dirty": 0,
        "zones_with_available": 0,
        "waiting": len(waitlist_entries),
    }

    for zona in zonas:
        tables = list(zona.mesas)
        ordered_tables = sorted(
            tables,
            key=lambda mesa: (
                2
                if active_orders.get(mesa.id)
                else 1
                if mesa.limpieza_estado != "limpia"
                else 0,
                mesa.numero,
                mesa.etiqueta,
            ),
        )
        available_count = sum(
            1
            for mesa in tables
            if not active_orders.get(mesa.id) and mesa.limpieza_estado == "limpia"
        )
        occupied_count = sum(1 for mesa in tables if active_orders.get(mesa.id))
        dirty_count = sum(
            1
            for mesa in tables
            if not active_orders.get(mesa.id) and mesa.limpieza_estado == "sucia"
        )
        active_account_count = sum(len(active_orders.get(mesa.id, [])) for mesa in tables)

        zone_cards.append(
            {
                "zona": zona,
                "tables": ordered_tables,
                "total": len(tables),
                "available": available_count,
                "occupied": occupied_count,
                "active_accounts": active_account_count,
                "dirty": dirty_count,
            }
        )

        table_summary["total"] += len(tables)
        table_summary["available"] += available_count
        table_summary["occupied"] += occupied_count
        table_summary["active_accounts"] += active_account_count
        table_summary["dirty"] += dirty_count
        if available_count:
            table_summary["zones_with_available"] += 1

    return render_template(
        "tables.html",
        page_title="Mesas",
        zonas=zonas,
        zone_cards=zone_cards,
        table_summary=table_summary,
        active_orders=active_orders,
        waitlist_entries=waitlist_entries,
        cash_session=get_active_cash_session(),
    )


@web_bp.post("/lista-espera")
@feature_required("ordenes.create")
def crear_lista_espera():
    waitlist_url = url_for("web.mesas", modal="waitlist")
    personas = parse_int(request.form.get("personas"))
    nombre_cliente = (request.form.get("nombre_cliente") or "").strip()
    telefono = (request.form.get("telefono") or "").strip()
    notas = (request.form.get("notas") or "").strip()

    if personas <= 0 or personas > 60:
        flash("Indica para cuantas personas es el grupo en espera.", "error")
        return redirect(waitlist_url)

    if not nombre_cliente:
        nombre_cliente = f"Grupo de {personas}"

    entry = ListaEspera(
        nombre_cliente=nombre_cliente[:100],
        personas=personas,
        telefono=telefono[:40] or None,
        notas=notas[:200] or None,
        usuario_id=current_user.id,
    )
    db.session.add(entry)
    audit_event(
        "crear",
        "lista_espera",
        None,
        f"Se agrego {entry.nombre_cliente} a lista de espera.",
        {"personas": personas},
    )
    db.session.commit()

    flash(f"{entry.nombre_cliente} quedo en lista de espera para {entry.etiqueta_personas}.", "success")
    return redirect(waitlist_url)


@web_bp.post("/lista-espera/<int:entry_id>/cancelar")
@feature_required("ordenes.create")
def cancelar_lista_espera(entry_id):
    waitlist_url = url_for("web.mesas", modal="waitlist")
    entry = db.session.get(ListaEspera, entry_id)
    if entry is None:
        flash("La entrada de lista de espera no existe.", "error")
        return redirect(waitlist_url)
    if entry.estado != "esperando":
        flash("Ese grupo ya no esta esperando.", "warning")
        return redirect(waitlist_url)

    entry.estado = "cancelado"
    entry.closed_at = datetime.utcnow()
    audit_event(
        "cancelar",
        "lista_espera",
        entry.id,
        f"Se cancelo la espera de {entry.nombre_cliente}.",
        {"personas": entry.personas},
    )
    db.session.commit()

    flash(f"Se retiro a {entry.nombre_cliente} de la lista de espera.", "success")
    return redirect(waitlist_url)


@web_bp.post("/lista-espera/<int:entry_id>/sentar")
@feature_required("ordenes.create")
def sentar_lista_espera(entry_id):
    waitlist_url = url_for("web.mesas", modal="waitlist")
    entry = db.session.get(ListaEspera, entry_id)
    if entry is None:
        flash("La entrada de lista de espera no existe.", "error")
        return redirect(waitlist_url)
    if entry.estado != "esperando":
        flash("Ese grupo ya no esta esperando.", "warning")
        return redirect(waitlist_url)

    cash_session = get_active_cash_session()
    if cash_session is None:
        flash("Primero abre caja para poder sentar grupos y crear ordenes.", "error")
        return redirect(url_for("web.caja"))

    mesa_id = parse_int(request.form.get("mesa_id"))
    mesa = db.session.get(Mesa, mesa_id)
    if mesa is None or mesa.id == current_app.config["TAKEOUT_TABLE_ID"]:
        flash("Selecciona una mesa valida para sentar al grupo.", "error")
        return redirect(waitlist_url)
    if mesa.limpieza_estado == "sucia":
        flash("No puedes sentar un grupo en una mesa marcada como sucia.", "warning")
        return redirect(waitlist_url)
    if get_active_order_for_mesa(mesa.id):
        flash("Selecciona una mesa libre para sentar a un grupo de lista de espera.", "warning")
        return redirect(waitlist_url)

    order = Orden(
        mesa_id=mesa.id,
        sesion_caja_id=cash_session.id,
        usuario_id=current_user.id,
        nombre_cliente=entry.nombre_cliente,
    )
    mesa.estado = "ocupada"
    entry.estado = "sentado"
    entry.mesa_id = mesa.id
    entry.closed_at = datetime.utcnow()

    db.session.add(order)
    audit_event(
        "sentar",
        "lista_espera",
        entry.id,
        f"Se sento {entry.nombre_cliente} en {mesa.etiqueta}.",
        {"mesa_id": mesa.id, "personas": entry.personas},
    )
    db.session.commit()

    flash(f"{entry.nombre_cliente} fue sentado en {mesa.etiqueta}: orden #{order.id}.", "success")
    return redirect(url_for("web.detalle_orden", order_id=order.id))


@web_bp.get("/mesas/nueva")
@feature_required("mesas.create")
def nueva_mesa():
    return render_template(
        "table_form.html",
        page_title="Nueva mesa",
        mesa=None,
        zonas=get_zonas(),
    )


@web_bp.post("/mesas")
@feature_required("mesas.create")
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
    audit_event("crear", "mesa", None, f"Se creo {mesa.etiqueta}.")
    db.session.commit()

    flash(f"Se creo {mesa.etiqueta}.", "success")
    return redirect(url_for("web.mesas"))


@web_bp.get("/mesas/<int:mesa_id>/editar")
@feature_required("mesas.edit")
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
@feature_required("mesas.edit")
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
    audit_event("actualizar", "mesa", mesa.id, f"Se actualizo {mesa.etiqueta}.")
    db.session.commit()

    flash(f"Se actualizo {mesa.etiqueta}.", "success")
    return redirect(url_for("web.mesas"))


@web_bp.post("/mesas/<int:mesa_id>/limpieza")
@feature_required("mesas.edit")
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
    audit_event("actualizar_limpieza", "mesa", mesa.id, f"{mesa.etiqueta} cambio a {limpieza_estado}.")
    db.session.commit()
    flash(
        f"{mesa.etiqueta} quedó marcada como {'limpia' if limpieza_estado == 'limpia' else 'sucia'}.",
        "success",
    )
    return redirect(request.referrer or url_for("web.mesas"))


@web_bp.post("/mesas/limpieza/masiva")
@feature_required("mesas.edit")
def limpiar_mesas_masivo():
    mesa_ids = {
        parse_int(value)
        for value in request.form.getlist("mesa_ids")
        if parse_int(value) > 0
    }
    if not mesa_ids:
        flash("Selecciona al menos una mesa sucia para limpiar.", "warning")
        return redirect(request.referrer or url_for("web.mesas"))

    mesas = (
        Mesa.query.filter(Mesa.id.in_(mesa_ids))
        .filter(Mesa.limpieza_estado == "sucia")
        .filter(Mesa.estado != "ocupada")
        .all()
    )
    if not mesas:
        flash("No hay mesas seleccionadas que puedan marcarse como limpias.", "warning")
        return redirect(request.referrer or url_for("web.mesas"))

    cleaned_labels = []
    for mesa in mesas:
        mesa.limpieza_estado = "limpia"
        cleaned_labels.append(mesa.etiqueta)

    audit_event(
        "limpieza_masiva",
        "mesa",
        ",".join(str(mesa.id) for mesa in mesas),
        f"Se marcaron {len(mesas)} mesas como limpias.",
        {"mesas": cleaned_labels},
    )
    db.session.commit()

    flash(
        f"Se marcaron {len(mesas)} mesa{'' if len(mesas) == 1 else 's'} como limpia{'' if len(mesas) == 1 else 's'}.",
        "success",
    )
    return redirect(request.referrer or url_for("web.mesas"))


@web_bp.post("/mesas/<int:mesa_id>/eliminar")
@feature_required("mesas.delete")
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
    audit_event("eliminar", "mesa", mesa_id, f"Se elimino {mesa_label}.")
    db.session.commit()
    flash(f"Se eliminó {mesa_label}.", "success")
    return redirect(url_for("web.mesas"))


@web_bp.get("/ordenes")
@feature_required("ordenes.view")
def ordenes():
    status = request.args.get("estado") or None
    filter_date = parse_date_filter(request.args.get("fecha"))
    orders = get_orders_for_listing(status=status, date_value=filter_date)
    available_tables = get_mesas_disponibles()
    _, active_orders = grouped_tables()
    return render_template(
        "orders.html",
        page_title="Ordenes",
        orders=orders,
        available_tables=available_tables,
        active_orders=active_orders,
        selected_status=status or "",
        selected_date=filter_date.isoformat(),
        cash_session=get_active_cash_session(),
    )


@web_bp.post("/ordenes")
@feature_required("ordenes.create")
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

    was_occupied = not is_takeout and mesa.estado == "ocupada"
    order = Orden(
        mesa_id=mesa.id,
        sesion_caja_id=cash_session.id,
        usuario_id=current_user.id,
        nombre_cliente=nombre_cliente or None,
    )
    if not is_takeout:
        mesa.estado = "ocupada"
    db.session.add(order)
    audit_event("crear", "orden", None, f"Se abrio una orden en {mesa.etiqueta}.")
    db.session.commit()

    if was_occupied:
        flash(f"Se abrio una cuenta separada en {mesa.etiqueta}: orden #{order.id}.", "success")
    else:
        flash(f"Se abrio la orden #{order.id}.", "success")
    return redirect(url_for("web.detalle_orden", order_id=order.id))


@web_bp.get("/ordenes/<int:order_id>")
@feature_required("ordenes.view")
def detalle_orden(order_id):
    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    state_changed = normalize_item_delivery_states(order)
    items_consolidated = consolidate_order_items(order)
    if state_changed or items_consolidated:
        sync_order(order)
        db.session.commit()

    requested_people = parse_int(request.args.get("personas"), 0)
    split_mode = bool(order.divisiones) or bool_from_form(request.args.get("split"))
    division_count = len(order.divisiones)
    people_count = division_count or requested_people or 2
    people_count = max(2, min(10, people_count))

    can_charge, charge_message = order_can_receive_payment(current_user, order)
    products = get_productos(disponibles_only=True)

    return render_template(
        "order_detail.html",
        page_title=f"Orden #{order.id}",
        order=order,
        products=products,
        quick_stock_available={
            product.id: available_stock_units(product)
            for product in products
            if product.controla_stock
        },
        low_stock_threshold=LOW_STOCK_THRESHOLD,
        split_mode=split_mode,
        split_people_count=people_count,
        split_matrix=build_split_matrix(order, people_count),
        split_labels={
            division.numero_persona: division.etiqueta or ""
            for division in order.divisiones
        },
        can_charge=can_charge,
        charge_message=charge_message,
        can_prepare=user_can(current_user, "cocina"),
        can_deliver=user_can(current_user, "ordenes"),
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
    if order.estado != "pagada":
        flash("El ticket de venta solo esta disponible cuando la orden ya fue pagada.", "warning")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    return render_template(
        "ticket_receipt.html",
        order=order,
        ticket_lines=ticket_lines_for_order(order),
        ticket_payment=ticket_payment_context(order),
        ticket_total=order.total,
        ticket_number=str(order.id),
        ticket_label=None,
        printed_by=current_user.nombre_completo or current_user.nickname,
    )


@web_bp.get("/divisiones/<int:division_id>/ticket")
@login_required
def ticket_division(division_id):
    if not allow_ticket_access():
        flash("No tienes permiso para ver tickets.", "error")
        return redirect(url_for(default_endpoint_for_user(current_user)))

    division = (
        OrdenDivision.query.options(
            joinedload(OrdenDivision.orden).joinedload(Orden.mesa),
            joinedload(OrdenDivision.items)
            .joinedload(OrdenDivisionItem.orden_item)
            .joinedload(OrdenItem.producto),
        )
        .filter_by(id=division_id)
        .first()
    )
    if division is None:
        flash("La cuenta separada no existe.", "error")
        return redirect(url_for("web.ordenes"))
    if not division.pagada:
        flash("El ticket de una cuenta separada solo esta disponible cuando ya fue pagada.", "warning")
        return redirect(url_for("web.detalle_orden", order_id=division.orden_id))

    return render_template(
        "ticket_receipt.html",
        order=division.orden,
        ticket_lines=ticket_lines_for_division(division),
        ticket_payment=ticket_payment_context(
            division.orden,
            total=division.total,
            division_id=division.id,
        ),
        ticket_total=division.total,
        ticket_number=f"{division.orden_id}-{division.numero_persona}",
        ticket_label=division.nombre_visible,
        printed_by=current_user.nombre_completo or current_user.nickname,
    )


@web_bp.post("/ordenes/<int:order_id>/items")
@feature_required("ordenes.items")
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

    stock_errors = stock_request_errors(selected_lines)
    if stock_errors:
        flash(stock_errors[0], "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    changed, message = reset_divisiones_if_possible(order)
    if message:
        flash(message, "info" if changed else "error")
        if not changed and order.divisiones:
            return redirect(url_for("web.detalle_orden", order_id=order.id))

    merged_lines = 0
    for line in selected_lines:
        product = line["product"]
        _, merged = add_or_increment_order_item(
            order,
            product,
            line["quantity"],
            line["notes"],
        )
        if merged:
            merged_lines += 1

    db.session.flush()
    sync_order(order)
    audit_event(
        "agregar_items",
        "orden",
        order.id,
        f"Se agregaron {len(selected_lines)} linea(s) a la orden #{order.id}.",
    )
    db.session.commit()

    for line in selected_lines:
        product = line["product"]
        remaining = available_stock_units(product)
        if remaining is not None and remaining <= LOW_STOCK_THRESHOLD:
            flash(f"Stock bajo: {product.nombre} queda con {remaining} unidades disponibles.", "warning")

    if len(selected_lines) == 1:
        line = selected_lines[0]
        if merged_lines:
            flash(f"Se sumo {line['product'].nombre} x{line['quantity']} a la linea existente.", "success")
        else:
            flash(f"Se agrego {line['product'].nombre} x{line['quantity']}.", "success")
    else:
        total_units = sum(line["quantity"] for line in selected_lines)
        if merged_lines:
            flash(f"Se agregaron {len(selected_lines)} productos ({total_units} unidades), sumando repetidos.", "success")
        else:
            flash(f"Se agregaron {len(selected_lines)} productos ({total_units} unidades).", "success")
    return redirect(url_for("web.detalle_orden", order_id=order.id))


@web_bp.post("/items/<int:item_id>/preparar")
@feature_required("cocina.prepare")
def preparar_item(item_id):
    item, redirect_response = get_item_or_redirect(item_id)
    if redirect_response:
        return redirect_response

    if not item_can_be_prepared(current_user, item):
        flash("Ese item no puede pasar a listo desde tu rol o estado actual.", "error")
        return redirect(request.referrer or url_for("web.cocina"))

    item.estado = "entregado"
    settle_order(item.orden)
    audit_event("preparar_item", "orden_item", item.id, f"Item #{item.id} preparado.")
    db.session.commit()
    flash("El item quedo listo y entregado.", "success")
    return redirect(request.referrer or url_for("web.cocina"))


@web_bp.post("/items/<int:item_id>/entregar")
@feature_required("ordenes.deliver")
def entregar_item(item_id):
    item, redirect_response = get_item_or_redirect(item_id)
    if redirect_response:
        return redirect_response

    if not item_can_be_delivered(current_user, item):
        flash("Ese item no esta listo para entregar.", "error")
        return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))

    item.estado = "entregado"
    settle_order(item.orden)
    audit_event("entregar_item", "orden_item", item.id, f"Item #{item.id} entregado.")
    db.session.commit()

    flash("El item fue marcado como entregado.", "success")
    return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))


@web_bp.post("/items/<int:item_id>/cancelar")
@feature_required("ordenes.cancel_item")
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
    if item.estado == "entregado" and not user_can(current_user, "usuarios"):
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

    cancel_quantity = parse_int(request.form.get("cancel_quantity"), item.cantidad)
    cancel_quantity = max(1, min(cancel_quantity, item.cantidad))
    original_quantity = item.cantidad

    if cancel_quantity >= item.cantidad:
        item.estado = "cancelado"
    else:
        item.cantidad -= cancel_quantity

    sync_order(item.orden)
    settle_order(item.orden)
    audit_event(
        "cancelar_item",
        "orden_item",
        item.id,
        f"Se cancelaron {cancel_quantity} de {original_quantity} unidades del item #{item.id}.",
    )
    db.session.commit()

    if cancel_quantity >= original_quantity:
        flash("El item fue cancelado.", "success")
    else:
        flash(f"Se cancelaron {cancel_quantity} unidades del item.", "success")
    return redirect(request.referrer or url_for("web.detalle_orden", order_id=item.orden_id))


@web_bp.post("/ordenes/<int:order_id>/dividir")
@feature_required("caja.charge")
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
    audit_event("dividir", "orden", order.id, f"Orden #{order.id} dividida en {people_count} cuentas.")
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
@feature_required("caja.charge")
def quitar_division(order_id):
    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    if any(division.pagada for division in order.divisiones):
        flash("No puedes quitar la division porque ya hay personas cobradas.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    clear_divisiones(order)
    audit_event("quitar_division", "orden", order.id, f"Se quito la division de orden #{order.id}.")
    db.session.commit()
    flash("La orden volvio al cobro normal.", "success")
    return redirect(url_for("web.detalle_orden", order_id=order.id))


@web_bp.post("/divisiones/<int:division_id>/pagar")
@feature_required("caja.charge")
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
    stock_errors = order_stock_errors(division.orden)
    if stock_errors:
        flash(stock_errors[0], "error")
        return redirect(url_for("web.detalle_orden", order_id=division.orden_id))

    payment = Pago(orden=division.orden, metodo=method, monto=division.total)
    division.pagada = True
    db.session.add(payment)
    db.session.flush()
    settle_order(division.orden)
    audit_event("cobrar", "orden", division.orden_id, f"Se cobro {division.nombre_visible}.")
    db.session.commit()
    remember_ticket_payment(division.orden_id, division.total, method, division_id=division.id)
    if division.orden.estado == "pagada":
        flash_low_stock_for_order(division.orden)

    flash(f"{division.nombre_visible} quedo cobrada.", "success")
    return redirect(url_for("web.detalle_orden", order_id=division.orden_id))


@web_bp.post("/ordenes/<int:order_id>/pagar")
@feature_required("caja.charge")
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
    stock_errors = order_stock_errors(order)
    if stock_errors:
        flash(stock_errors[0], "error")
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
    audit_event("cobrar", "orden", order.id, f"Se registro pago en orden #{order.id}.", {"monto": amount, "metodo": method})
    db.session.commit()
    remember_ticket_payment(order.id, amount, method)
    if order.estado == "pagada":
        flash_low_stock_for_order(order)

    if order.estado == "pagada":
        flash(f"La orden #{order.id} quedo pagada.", "success")
    else:
        flash("Pago registrado.", "success")
    return redirect(url_for("web.detalle_orden", order_id=order.id))


@web_bp.post("/ordenes/<int:order_id>/cancelar")
@feature_required("caja.cancel_order")
def cancelar_orden(order_id):
    order = get_order(order_id)
    if order is None:
        flash("La orden no existe.", "error")
        return redirect(url_for("web.ordenes"))

    if order.total_pagado > 0:
        flash("No puedes cancelar una orden que ya tiene pagos.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))
    if any(item.estado == "entregado" for item in order.items_activos) and not user_can(
        current_user, "usuarios"
    ):
        flash("Solo administracion puede cancelar una orden con items ya entregados.", "error")
        return redirect(url_for("web.detalle_orden", order_id=order.id))

    order.estado = "cancelada"
    for item in order.items:
        if item.estado != "cancelado":
            item.estado = "cancelado"
    sync_order(order)
    audit_event("cancelar", "orden", order.id, f"Orden #{order.id} cancelada.")
    db.session.commit()

    flash(f"La orden #{order.id} fue cancelada.", "success")
    return redirect(url_for("web.ordenes"))


@web_bp.get("/productos")
@feature_required("productos.view")
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
@feature_required("productos.create")
def nuevo_producto():
    return render_template(
        "product_form.html",
        page_title="Nuevo producto",
        product=None,
        categories=get_categorias(),
    )


@web_bp.post("/productos")
@feature_required("productos.create")
def crear_producto():
    payload, errors = extract_product_payload()
    if errors:
        flash_form_errors(errors)
        return redirect(url_for("web.nuevo_producto"))

    product = Producto(**payload)
    db.session.add(product)
    audit_event("crear", "producto", None, f"Se creo el producto {product.nombre}.")
    db.session.commit()

    flash(f"Se creo el producto {product.nombre}.", "success")
    return redirect(url_for("web.productos"))


@web_bp.get("/productos/<int:product_id>/editar")
@feature_required("productos.edit")
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
@feature_required("productos.edit")
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

    audit_event("actualizar", "producto", product.id, f"Se actualizo el producto {product.nombre}.")
    db.session.commit()

    new_image = payload.get("imagen_url")
    if previous_image and previous_image != new_image:
        delete_uploaded_product_image(previous_image)

    flash(f"Se actualizo el producto {product.nombre}.", "success")
    return redirect(url_for("web.productos"))


@web_bp.post("/productos/<int:product_id>/disponibilidad")
@feature_required("productos.availability")
def alternar_disponibilidad_producto(product_id):
    product = get_producto(product_id)
    if product is None:
        flash("El producto no existe.", "error")
        return redirect(url_for("web.productos"))

    product.disponible = not product.disponible
    audit_event(
        "cambiar_disponibilidad",
        "producto",
        product.id,
        f"{product.nombre} quedo {'disponible' if product.disponible else 'agotado'}.",
    )
    db.session.commit()

    status = "disponible" if product.disponible else "agotado"
    flash(f"Se marco {product.nombre} como {status}.", "success")

    search = (request.form.get("q") or "").strip()
    if search:
        return redirect(url_for("web.productos", q=search))
    return redirect(url_for("web.productos"))


@web_bp.post("/productos/<int:product_id>/eliminar")
@feature_required("productos.delete")
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
    audit_event("eliminar", "producto", product_id, f"Se elimino el producto {product_name}.")
    db.session.commit()

    delete_uploaded_product_image(image_to_delete)
    flash(f"Se elimino el producto {product_name}.", "success")
    return redirect(url_for("web.productos"))


@web_bp.get("/caja")
@feature_required("caja.view")
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
@feature_required("caja.open")
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
    audit_event("abrir", "caja", None, "Caja abierta.", {"monto_apertura": opening_amount})
    db.session.commit()

    flash("Caja abierta correctamente.", "success")
    return redirect(url_for("web.caja"))


@web_bp.post("/caja/movimientos")
@feature_required("caja.movements")
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
    audit_event("movimiento", "caja", session_open.id, f"Movimiento de caja: {concept}.", {"tipo": movement_type, "monto": amount})
    db.session.commit()

    flash("Movimiento registrado.", "success")
    return redirect(url_for("web.caja"))


@web_bp.post("/caja/cerrar")
@feature_required("caja.close")
def cerrar_caja():
    session_open = get_active_cash_session()
    if session_open is None:
        flash("No hay una caja abierta para cerrar.", "error")
        return redirect(url_for("web.caja"))

    closing_amount = parse_decimal(request.form.get("closing_amount"))
    session_open.monto_cierre_real = closing_amount
    session_open.fecha_cierre = datetime.utcnow()
    session_open.estado = "cerrada"
    audit_event("cerrar", "caja", session_open.id, "Caja cerrada.", {"monto_cierre": closing_amount})
    db.session.commit()

    flash("Caja cerrada correctamente.", "success")
    return redirect(url_for("web.caja"))


@web_bp.get("/reportes")
@feature_required("reportes.view")
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
@feature_required("reportes.export")
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
@feature_required("reportes.export")
def exportar_reporte(kind):
    start_date, end_date = parse_report_range()

    if kind == "ventas":
        payments = get_payments_for_range(start_date, end_date)
        return build_csv_response(
            f"ventas_{start_date.isoformat()}_{end_date.isoformat()}.csv",
            ["Fecha", "Orden", "Mesa", "Cliente", "Metodo", "Monto"],
            [
                [
                    local_datetime_label(payment.created_at),
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
                "Precio venta unidad",
                "Costo unitario estimado",
                "Unidad compra",
                "Unidades por paquete",
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
                    product.unidad_compra,
                    product.unidades_por_paquete,
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
            ["Fecha", "Producto", "Tipo", "Paquetes", "Unidades", "Precio referencia", "Usuario", "Notas"],
            [
                [
                    local_datetime_label(movement.created_at),
                    movement.producto.nombre if movement.producto else "",
                    movement.tipo,
                    movement.cantidad_paquetes or "",
                    movement.cantidad_unidades,
                    f"{movement.precio_unitario or ''}",
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
                    local_datetime_label(order.created_at),
                ]
                for order in orders
            ],
        )

    flash("Ese reporte no existe.", "error")
    return redirect(url_for("web.reportes"))


@web_bp.get("/cocina")
@feature_required("cocina.view")
def cocina():
    return render_template(
        "kitchen.html",
        page_title="Cocina",
        pending_items=get_pending_kitchen_items(),
        prepare_url_template=url_for("web.preparar_item", item_id=0).replace("0", "__ID__"),
    )


@web_bp.get("/inventario")
@feature_required("inventario.view")
def inventario():
    return render_template(
        "inventory.html",
        page_title="Inventario",
        products=get_inventory_products(),
        movements=recent_inventory_movements(),
        low_stock_products=get_low_stock_products(limit=8),
    )


@web_bp.get("/inventario/nuevo")
@feature_required("inventario.create")
def nuevo_movimiento_inventario():
    return render_template(
        "inventory_form.html",
        page_title="Nuevo movimiento de inventario",
        products=get_inventory_products(),
    )


@web_bp.post("/inventario")
@feature_required("inventario.create")
def registrar_movimiento_inventario():
    product_id = parse_int(request.form.get("product_id"))
    movement_type = request.form.get("movement_type")
    packages_count = parse_int(request.form.get("packages"), default=0)
    units = parse_int(request.form.get("units"), default=0)
    package_units = parse_int(request.form.get("package_units"), default=None)
    reference_price = parse_decimal(request.form.get("unit_price"), default=None)
    sale_price = parse_decimal(request.form.get("sale_price"), default=None)
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

    cash_movement = None

    if movement_type == "compra":
        if packages_count < 0 or units < 0:
            flash("En una compra, paquetes y unidades no pueden ser negativos.", "error")
            return redirect(url_for("web.nuevo_movimiento_inventario"))

        package_units = max(package_units or product.unidades_por_paquete or 1, 1)
        stored_units = (packages_count * package_units) + units
        if stored_units <= 0:
            flash("Ingresa paquetes o unidades para registrar la compra.", "error")
            return redirect(url_for("web.nuevo_movimiento_inventario"))

        if reference_price is not None and reference_price > 0:
            session_open = get_active_cash_session()
            if session_open is None:
                flash("Abre caja antes de registrar compras con costo.", "error")
                return redirect(url_for("web.nuevo_movimiento_inventario"))

            unit_cost = normalize_money(reference_price / package_units)
            purchase_total = normalize_money(
                (reference_price * packages_count) + (unit_cost * units)
            )
            cash_movement = MovimientoCaja(
                sesion_caja_id=session_open.id,
                tipo="egreso",
                concepto=(
                    f"Compra inventario: {product.nombre} "
                    f"({packages_count} paquetes, {stored_units} unidades)"
                ),
                monto=purchase_total,
            )

        product.stock_actual += stored_units
        product.unidades_por_paquete = package_units

        if reference_price is not None and reference_price > 0:
            product.precio_costo = unit_cost
        if sale_price is not None and sale_price > 0:
            product.precio_venta = sale_price
    elif movement_type == "venta":
        if packages_count < 0:
            flash("Los paquetes no pueden ser negativos.", "error")
            return redirect(url_for("web.nuevo_movimiento_inventario"))
        package_units = max(package_units or product.unidades_por_paquete or 1, 1)
        stored_units = (max(packages_count, 0) * package_units) + abs(units)
        if stored_units <= 0:
            flash("Ingresa unidades o paquetes para registrar la venta manual.", "error")
            return redirect(url_for("web.nuevo_movimiento_inventario"))
        if stored_units > product.stock_actual:
            flash(
                f"No puedes vender {stored_units} unidades de {product.nombre}; solo hay {product.stock_actual}.",
                "error",
            )
            return redirect(url_for("web.nuevo_movimiento_inventario"))

        effective_sale_price = (
            sale_price if sale_price is not None and sale_price > 0 else product.precio_venta
        )
        if effective_sale_price is None or effective_sale_price <= 0:
            flash("Ingresa el precio de venta por unidad para registrar la venta manual.", "error")
            return redirect(url_for("web.nuevo_movimiento_inventario"))

        session_open = get_active_cash_session()
        if session_open is None:
            flash("Abre caja antes de registrar una venta manual.", "error")
            return redirect(url_for("web.nuevo_movimiento_inventario"))

        sale_total = normalize_money(effective_sale_price * stored_units)
        cash_movement = MovimientoCaja(
            sesion_caja_id=session_open.id,
            tipo="ingreso",
            concepto=f"Venta manual inventario: {product.nombre} ({stored_units} unidades)",
            monto=sale_total,
        )
        reference_price = effective_sale_price
        product.stock_actual -= stored_units
    else:
        if sale_price is None or sale_price <= 0:
            flash("Ingresa el nuevo precio de venta por unidad.", "error")
            return redirect(url_for("web.nuevo_movimiento_inventario"))

        product.precio_venta = sale_price
        stored_units = 0
        reference_price = sale_price
        if not notes:
            notes = f"Ajuste de precio de venta a ${sale_price:.2f}"

    movement = MovimientoInventario(
        producto_id=product.id,
        tipo=movement_type,
        cantidad_paquetes=(
            packages_count if movement_type != "ajuste" and packages_count else None
        ),
        cantidad_unidades=stored_units,
        precio_unitario=reference_price,
        notas=notes or None,
        usuario_id=current_user.id,
    )
    db.session.add(movement)
    if cash_movement is not None:
        db.session.add(cash_movement)
    audit_event(
        "movimiento",
        "inventario",
        product.id,
        f"Movimiento de inventario en {product.nombre}.",
        {"tipo": movement_type, "unidades": stored_units},
    )
    if cash_movement is not None:
        audit_event(
            "movimiento",
            "caja",
            cash_movement.sesion_caja_id,
            (
                f"Egreso automatico por compra de inventario: {product.nombre}."
                if movement_type == "compra"
                else f"Ingreso automatico por venta manual: {product.nombre}."
            ),
            {"monto": cash_movement.monto, "producto_id": product.id},
        )
    db.session.commit()

    if movement_type == "compra" and cash_movement is not None:
        flash(
            f"Movimiento de inventario registrado y egreso de caja por ${cash_movement.monto:.2f}.",
            "success",
        )
    elif movement_type == "venta" and cash_movement is not None:
        flash(
            f"Venta manual registrada e ingreso de caja por ${cash_movement.monto:.2f}.",
            "success",
        )
    elif movement_type == "ajuste":
        flash(
            f"Ajuste de {product.nombre} registrado correctamente.",
            "success",
        )
    else:
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
    if not user_can(current_user, "dashboard"):
        return api_permission_denied()

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
    if not user_can(current_user, "mesas"):
        return api_permission_denied()

    try:
        zonas, active_orders = grouped_tables()
        payload = []
        for zona in zonas:
            for mesa in zona.mesas:
                table_orders = active_orders.get(mesa.id, [])
                active_order = table_orders[0] if table_orders else None
                data = mesa.to_dict()
                data["orden_abierta_id"] = active_order.id if active_order else None
                data["orden_total"] = float(active_order.total) if active_order else 0
                data["ordenes_abiertas"] = len(table_orders)
                data["ordenes_total"] = sum(float(order.total) for order in table_orders)
                payload.append(data)
        return jsonify(payload)
    except SQLAlchemyError as exc:
        return database_error_response(exc)


@api_bp.get("/productos")
@login_required
def api_productos():
    if not user_can(current_user, "productos"):
        return api_permission_denied()

    try:
        return jsonify([product.to_dict() for product in get_productos()])
    except SQLAlchemyError as exc:
        return database_error_response(exc)


@api_bp.get("/ordenes/abiertas")
@login_required
def api_ordenes_abiertas():
    if not user_can(current_user, "ordenes"):
        return api_permission_denied()

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
            "can_prepare": user_can(current_user, "cocina"),
            "can_deliver": user_can(current_user, "ordenes"),
            "division_locked": division_locked,
            "owner_user": user_can(current_user, "usuarios"),
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
                "order_state": order.estado,
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
