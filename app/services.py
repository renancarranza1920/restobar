from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import inspect
from sqlalchemy.orm import joinedload, selectinload

from .extensions import db
from .models import (
    Categoria,
    Mesa,
    MovimientoCaja,
    MovimientoInventario,
    Orden,
    OrdenDivision,
    OrdenDivisionItem,
    OrdenItem,
    Pago,
    Producto,
    SesionCaja,
    Usuario,
    Zona,
    as_decimal,
)


CENTAVOS = Decimal("0.01")
ZERO = Decimal("0.00")
LOW_STOCK_THRESHOLD = 5

FEATURES_BY_ROLE = {
    "dueño": {
        "dashboard",
        "mesas",
        "zonas",
        "categorias",
        "ordenes",
        "productos",
        "caja",
        "cocina",
        "inventario",
        "usuarios",
        "reportes",
    },
    "cajero": {"dashboard", "mesas", "ordenes", "caja"},
    "mesero": {"mesas", "ordenes"},
    "cocina": {"cocina"},
}

NAV_ITEMS = [
    {
        "feature": "dashboard",
        "label": "Inicio",
        "endpoint": "web.dashboard",
        "active_endpoints": {"web.dashboard"},
    },
    {
        "feature": "mesas",
        "label": "Mesas",
        "endpoint": "web.mesas",
        "active_endpoints": {"web.mesas", "web.nueva_mesa", "web.editar_mesa"},
    },
    {
        "feature": "zonas",
        "label": "Zonas",
        "endpoint": "web.zonas",
        "active_endpoints": {"web.zonas", "web.nueva_zona", "web.editar_zona"},
    },
    {
        "feature": "categorias",
        "label": "Categorias",
        "endpoint": "web.categorias",
        "active_endpoints": {
            "web.categorias",
            "web.nueva_categoria",
            "web.editar_categoria",
        },
    },
    {
        "feature": "ordenes",
        "label": "Órdenes",
        "endpoint": "web.ordenes",
        "active_endpoints": {
            "web.ordenes",
            "web.detalle_orden",
            "web.ticket_orden",
            "web.ticket_cocina",
        },
    },
    {
        "feature": "productos",
        "label": "Productos",
        "endpoint": "web.productos",
        "active_endpoints": {
            "web.productos",
            "web.nuevo_producto",
            "web.editar_producto",
            "web.actualizar_producto",
        },
    },
    {
        "feature": "caja",
        "label": "Caja",
        "endpoint": "web.caja",
        "active_endpoints": {"web.caja"},
    },
    {
        "feature": "reportes",
        "label": "Reportes",
        "endpoint": "web.reportes",
        "active_endpoints": {"web.reportes", "web.exportar_reporte"},
    },
    {
        "feature": "cocina",
        "label": "Cocina",
        "endpoint": "web.cocina",
        "active_endpoints": {"web.cocina"},
    },
    {
        "feature": "inventario",
        "label": "Inventario",
        "endpoint": "web.inventario",
        "active_endpoints": {"web.inventario", "web.nuevo_movimiento_inventario"},
    },
    {
        "feature": "usuarios",
        "label": "Usuarios",
        "endpoint": "web.usuarios",
        "active_endpoints": {
            "web.usuarios",
            "web.nuevo_usuario",
            "web.editar_usuario",
        },
    },
]


def money(value):
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value.quantize(CENTAVOS, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(CENTAVOS, rounding=ROUND_HALF_UP)


def parse_decimal(value, default=ZERO):
    if value is None:
        return default

    cleaned = str(value).strip().replace("$", "").replace(",", "")
    if not cleaned:
        return default

    try:
        return money(cleaned)
    except (InvalidOperation, ValueError):
        return default


def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_date_value(raw_value, fallback):
    if not raw_value:
        return fallback
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return fallback


def bool_from_form(value):
    return str(value).strip().lower() in {"1", "true", "on", "si", "yes"}


def theme_choices():
    return {"light", "dark"}


def default_theme():
    return "light"


def app_timezone():
    timezone_name = current_app.config.get("APP_TIMEZONE", "America/El_Salvador")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo or timezone.utc


def localize_datetime(value):
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(app_timezone())


def local_now():
    return datetime.now(app_timezone())


def local_today():
    return local_now().date()


def role_label(role):
    labels = {
        "dueño": "Administrador",
        "cajero": "Cajero",
        "mesero": "Mesero",
        "cocina": "Cocina",
    }
    return labels.get(role, role)


def time_ago_label(value):
    if not value:
        return "--"

    localized_value = localize_datetime(value)
    delta = local_now() - localized_value
    total_seconds = max(int(delta.total_seconds()), 0)

    if total_seconds < 60:
        return "Ahora"

    total_minutes = total_seconds // 60
    if total_minutes < 60:
        return f"{total_minutes} min"

    total_hours = total_minutes // 60
    if total_hours < 24:
        return f"{total_hours} h"

    total_days = total_hours // 24
    return f"{total_days} d"


def user_can(user, feature):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return feature in FEATURES_BY_ROLE.get(user.rol, set())


def navigation_for_user(user):
    return [item for item in NAV_ITEMS if user_can(user, item["feature"])]


def default_endpoint_for_user(user):
    if not user:
        return "web.login"
    defaults = {
        "dueño": "web.dashboard",
        "cajero": "web.ordenes",
        "mesero": "web.ordenes",
        "cocina": "web.cocina",
    }
    return defaults.get(user.rol, "web.login")


def feature_required(feature):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not user_can(current_user, feature):
                flash("No tienes permiso para entrar en esta sección.", "error")
                return redirect(url_for(default_endpoint_for_user(current_user)))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.rol not in roles:
                flash("No tienes permiso para realizar esta acción.", "error")
                return redirect(url_for(default_endpoint_for_user(current_user)))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def is_safe_url(target):
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in {"http", "https"} and ref_url.netloc == test_url.netloc


def next_url_or_default(default_endpoint):
    next_url = request.args.get("next") or request.form.get("next")
    if is_safe_url(next_url):
        return next_url
    return url_for(default_endpoint)


def bootstrap_admin_account():
    inspector = inspect(db.engine)
    if not inspector.has_table("usuarios"):
        return

    admin_nickname = current_app.config["DEFAULT_ADMIN_NICKNAME"]
    default_admin_password = current_app.config["DEFAULT_ADMIN_PASSWORD"]

    admin = Usuario.query.filter_by(nickname=admin_nickname).first()
    changed = False

    if admin is None:
        admin = Usuario(
            nickname=admin_nickname,
            nombre="Admin",
            apellido="General",
            rol="dueño",
            activo=True,
        )
        admin.set_password(default_admin_password)
        db.session.add(admin)
        changed = True
    elif "cambiar_este_hash" in (admin.password_hash or ""):
        admin.set_password(default_admin_password)
        changed = True

    if changed:
        db.session.commit()


def bootstrap_takeout_table():
    inspector = inspect(db.engine)
    if not inspector.has_table("zonas") or not inspector.has_table("mesas"):
        return

    zone_name = current_app.config["TAKEOUT_ZONE_NAME"]
    table_id = current_app.config["TAKEOUT_TABLE_ID"]
    table_number = current_app.config["TAKEOUT_TABLE_NUMBER"]
    table_alias = current_app.config["TAKEOUT_TABLE_ALIAS"]

    zone = Zona.query.filter_by(nombre=zone_name).first()
    changed = False

    if zone is None:
        zone = Zona(nombre=zone_name)
        db.session.add(zone)
        db.session.flush()
        changed = True

    takeout_table = db.session.get(Mesa, table_id)
    if takeout_table is None:
        takeout_table = Mesa(
            id=table_id,
            numero=table_number,
            nombre_alias=table_alias,
            zona_id=zone.id,
            estado="disponible",
            limpieza_estado="limpia",
        )
        db.session.add(takeout_table)
        changed = True

    if changed:
        db.session.commit()


def is_takeout_table(mesa):
    return bool(mesa) and mesa.id == current_app.config["TAKEOUT_TABLE_ID"]


def get_active_cash_session():
    return (
        SesionCaja.query.options(
            joinedload(SesionCaja.usuario),
            selectinload(SesionCaja.movimientos),
            selectinload(SesionCaja.ordenes).selectinload(Orden.pagos),
        )
        .filter_by(estado="abierta")
        .order_by(SesionCaja.fecha_apertura.desc())
        .first()
    )


def get_user_by_nickname(nickname):
    return Usuario.query.filter_by(nickname=nickname).first()


def get_user(user_id):
    return db.session.get(Usuario, user_id)


def get_zonas():
    return (
        Zona.query.filter(Zona.nombre != current_app.config["TAKEOUT_ZONE_NAME"])
        .order_by(Zona.nombre.asc())
        .all()
    )


def get_zona(zona_id):
    return db.session.get(Zona, zona_id)


def get_mesas_disponibles():
    return (
        Mesa.query.options(joinedload(Mesa.zona))
        .filter_by(estado="disponible", limpieza_estado="limpia")
        .filter(Mesa.id != current_app.config["TAKEOUT_TABLE_ID"])
        .order_by(Mesa.numero.asc())
        .all()
    )


def get_categorias():
    return Categoria.query.order_by(Categoria.nombre.asc()).all()


def get_productos(disponibles_only=False, search=None):
    query = Producto.query.options(joinedload(Producto.categoria)).order_by(
        Producto.disponible.desc(), Producto.nombre.asc()
    )
    if disponibles_only:
        query = query.filter_by(disponible=True)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.join(Producto.categoria).filter(
            db.or_(Producto.nombre.like(pattern), Categoria.nombre.like(pattern))
        )
    return query.all()


def get_inventory_products():
    return (
        Producto.query.options(joinedload(Producto.categoria))
        .join(Producto.categoria)
        .filter(Producto.maneja_stock.is_(True))
        .filter(Categoria.envia_a_cocina.is_(False))
        .order_by(Producto.nombre.asc())
        .all()
    )


def get_producto(producto_id):
    return (
        Producto.query.options(joinedload(Producto.categoria))
        .filter_by(id=producto_id)
        .first()
    )


def get_low_stock_products(limit=6):
    return (
        Producto.query.options(joinedload(Producto.categoria))
        .join(Producto.categoria)
        .filter(Producto.maneja_stock.is_(True))
        .filter(Categoria.envia_a_cocina.is_(False))
        .filter(Producto.stock_actual <= LOW_STOCK_THRESHOLD)
        .order_by(Producto.stock_actual.asc(), Producto.nombre.asc())
        .limit(limit)
        .all()
    )


def get_active_order_for_mesa(mesa_id):
    return (
        Orden.query.options(
            joinedload(Orden.mesa).joinedload(Mesa.zona),
            joinedload(Orden.usuario),
            selectinload(Orden.items).joinedload(OrdenItem.producto).joinedload(
                Producto.categoria
            ),
            selectinload(Orden.pagos),
            selectinload(Orden.divisiones)
            .selectinload(OrdenDivision.items)
            .joinedload(OrdenDivisionItem.orden_item)
            .joinedload(OrdenItem.producto),
        )
        .filter_by(mesa_id=mesa_id, estado="abierta")
        .order_by(Orden.created_at.desc())
        .first()
    )


def get_order(order_id):
    return (
        Orden.query.options(
            joinedload(Orden.mesa).joinedload(Mesa.zona),
            joinedload(Orden.usuario),
            joinedload(Orden.sesion_caja),
            selectinload(Orden.items).joinedload(OrdenItem.producto).joinedload(
                Producto.categoria
            ),
            selectinload(Orden.pagos),
            selectinload(Orden.divisiones)
            .selectinload(OrdenDivision.items)
            .joinedload(OrdenDivisionItem.orden_item)
            .joinedload(OrdenItem.producto)
            .joinedload(Producto.categoria),
        )
        .filter_by(id=order_id)
        .first()
    )


def grouped_tables():
    zonas = (
        Zona.query.options(selectinload(Zona.mesas).joinedload(Mesa.zona))
        .filter(Zona.nombre != current_app.config["TAKEOUT_ZONE_NAME"])
        .order_by(Zona.nombre.asc())
        .all()
    )
    active_orders = (
        Orden.query.options(joinedload(Orden.mesa), selectinload(Orden.pagos))
        .filter_by(estado="abierta")
        .filter(Orden.mesa_id != current_app.config["TAKEOUT_TABLE_ID"])
        .all()
    )
    return zonas, {order.mesa_id: order for order in active_orders}


def get_orders_for_listing(status=None, date_value=None):
    query = Orden.query.options(
        joinedload(Orden.mesa).joinedload(Mesa.zona),
        joinedload(Orden.usuario),
        selectinload(Orden.pagos),
        selectinload(Orden.items).joinedload(OrdenItem.producto),
    ).order_by(Orden.created_at.desc())

    if status in {"abierta", "pagada", "cancelada"}:
        query = query.filter(Orden.estado == status)

    if date_value:
        query = query.filter(db.func.date(Orden.created_at) == date_value)

    return query.all()


def get_recent_orders(limit=8):
    return (
        Orden.query.options(joinedload(Orden.mesa), joinedload(Orden.usuario))
        .order_by(Orden.created_at.desc())
        .limit(limit)
        .all()
    )


def get_recent_payments(limit=8):
    return (
        Pago.query.options(joinedload(Pago.orden).joinedload(Orden.mesa))
        .order_by(Pago.created_at.desc())
        .limit(limit)
        .all()
    )


def get_orders_for_range(start_date, end_date):
    return (
        Orden.query.options(
            joinedload(Orden.mesa).joinedload(Mesa.zona),
            joinedload(Orden.usuario),
            selectinload(Orden.items).joinedload(OrdenItem.producto).joinedload(
                Producto.categoria
            ),
            selectinload(Orden.pagos),
        )
        .filter(db.func.date(Orden.created_at) >= start_date)
        .filter(db.func.date(Orden.created_at) <= end_date)
        .order_by(Orden.created_at.desc())
        .all()
    )


def get_payments_for_range(start_date, end_date):
    return (
        Pago.query.options(joinedload(Pago.orden).joinedload(Orden.mesa))
        .filter(db.func.date(Pago.created_at) >= start_date)
        .filter(db.func.date(Pago.created_at) <= end_date)
        .order_by(Pago.created_at.desc())
        .all()
    )


def get_inventory_for_range(start_date, end_date):
    return (
        MovimientoInventario.query.options(
            joinedload(MovimientoInventario.producto),
            joinedload(MovimientoInventario.usuario),
        )
        .filter(db.func.date(MovimientoInventario.created_at) >= start_date)
        .filter(db.func.date(MovimientoInventario.created_at) <= end_date)
        .order_by(MovimientoInventario.created_at.desc())
        .all()
    )


def build_top_products(orders, limit=8):
    rollup = {}
    total_items = 0
    total_cost = ZERO

    for order in orders:
        if order.estado != "pagada":
            continue

        for item in order.items_activos:
            if item.producto is None:
                continue

            total_items += item.cantidad
            item_cost = money(as_decimal(item.costo_unitario) * item.cantidad)
            total_cost += item_cost

            entry = rollup.setdefault(
                item.producto_id,
                {
                    "product": item.producto,
                    "quantity": 0,
                    "sales": ZERO,
                    "cost": ZERO,
                },
            )
            entry["quantity"] += item.cantidad
            entry["sales"] = money(entry["sales"] + item.subtotal)
            entry["cost"] = money(entry["cost"] + item_cost)

    rows = []
    for entry in rollup.values():
        entry["profit"] = money(entry["sales"] - entry["cost"])
        rows.append(entry)

    rows.sort(key=lambda item: (item["sales"], item["quantity"]), reverse=True)
    return rows[:limit], total_items, money(total_cost)


def get_report_snapshot(start_date, end_date):
    orders = get_orders_for_range(start_date, end_date)
    payments = get_payments_for_range(start_date, end_date)
    inventory = get_inventory_for_range(start_date, end_date)

    sales_total = money(sum((as_decimal(payment.monto) for payment in payments), ZERO))
    cash_total = money(
        sum(
            (as_decimal(payment.monto) for payment in payments if payment.metodo == "efectivo"),
            ZERO,
        )
    )
    card_total = money(
        sum(
            (as_decimal(payment.monto) for payment in payments if payment.metodo == "tarjeta"),
            ZERO,
        )
    )

    paid_orders_count = sum(1 for order in orders if order.estado == "pagada")
    open_orders_count = sum(1 for order in orders if order.estado == "abierta")
    cancelled_orders_count = sum(1 for order in orders if order.estado == "cancelada")
    top_products, items_sold, estimated_cost = build_top_products(orders)
    gross_profit = money(sales_total - estimated_cost)
    average_ticket = (
        money(sales_total / paid_orders_count) if paid_orders_count else ZERO
    )

    return {
        "start_date": start_date,
        "end_date": end_date,
        "orders": orders,
        "payments": payments,
        "inventory": inventory,
        "sales_total": sales_total,
        "cash_total": cash_total,
        "card_total": card_total,
        "estimated_cost": estimated_cost,
        "gross_profit": gross_profit,
        "average_ticket": average_ticket,
        "paid_orders_count": paid_orders_count,
        "open_orders_count": open_orders_count,
        "cancelled_orders_count": cancelled_orders_count,
        "items_sold": items_sold,
        "top_products": top_products,
        "low_stock_products": get_low_stock_products(limit=8),
    }


def get_dashboard_metrics():
    today = local_today()
    today_report = get_report_snapshot(today, today)
    mesas = Mesa.query.all()
    pendientes_cocina = (
        OrdenItem.query.join(OrdenItem.producto)
        .join(Producto.categoria)
        .join(OrdenItem.orden)
        .filter(Categoria.envia_a_cocina.is_(True))
        .filter(OrdenItem.estado == "pendiente")
        .filter(Orden.estado == "abierta")
        .count()
    )
    listos_entrega = (
        OrdenItem.query.join(OrdenItem.orden)
        .filter(Orden.estado == "abierta")
        .filter(OrdenItem.estado == "listo")
        .count()
    )

    return {
        "mesas_total": len(mesas),
        "mesas_ocupadas": len([mesa for mesa in mesas if mesa.estado == "ocupada"]),
        "productos_total": Producto.query.count(),
        "ordenes_abiertas": Orden.query.filter_by(estado="abierta").count(),
        "pendientes_cocina": pendientes_cocina,
        "listos_entrega": listos_entrega,
        "ventas_hoy": today_report["sales_total"],
        "efectivo_hoy": today_report["cash_total"],
        "tarjeta_hoy": today_report["card_total"],
        "ticket_promedio_hoy": today_report["average_ticket"],
        "utilidad_hoy": today_report["gross_profit"],
        "ordenes_pagadas_hoy": today_report["paid_orders_count"],
        "ordenes_canceladas_hoy": today_report["cancelled_orders_count"],
        "items_vendidos_hoy": today_report["items_sold"],
        "top_products": today_report["top_products"][:5],
        "low_stock_products": today_report["low_stock_products"][:5],
    }


def get_dashboard_snapshot():
    return {
        "metrics": get_dashboard_metrics(),
        "recent_orders": get_recent_orders(limit=8),
        "recent_payments": get_recent_payments(limit=8),
        "pending_kitchen_items": get_pending_kitchen_items(limit=6),
        "ready_for_delivery_items": get_ready_for_delivery_items(limit=6),
    }


def get_pending_kitchen_items(limit=None):
    query = (
        OrdenItem.query.options(
            joinedload(OrdenItem.producto).joinedload(Producto.categoria),
            joinedload(OrdenItem.orden).joinedload(Orden.mesa),
        )
        .join(OrdenItem.producto)
        .join(Producto.categoria)
        .join(OrdenItem.orden)
        .filter(Categoria.envia_a_cocina.is_(True))
        .filter(Orden.estado == "abierta")
        .filter(OrdenItem.estado == "pendiente")
        .order_by(OrdenItem.created_at.asc())
    )
    if limit:
        query = query.limit(limit)
    return query.all()


def get_ready_for_delivery_items(limit=None):
    query = (
        OrdenItem.query.options(
            joinedload(OrdenItem.producto).joinedload(Producto.categoria),
            joinedload(OrdenItem.orden).joinedload(Orden.mesa),
        )
        .join(OrdenItem.orden)
        .filter(Orden.estado == "abierta")
        .filter(OrdenItem.estado == "listo")
        .order_by(OrdenItem.created_at.asc())
    )
    if limit:
        query = query.limit(limit)
    return query.all()


def serialize_kitchen_item(item):
    return {
        "id": item.id,
        "order_id": item.orden_id,
        "table": item.orden.mesa.etiqueta if item.orden and item.orden.mesa else "-",
        "customer": item.orden.nombre_cliente if item.orden else None,
        "product": item.producto.nombre if item.producto else "-",
        "category": (
            item.producto.categoria.nombre
            if item.producto and item.producto.categoria
            else "-"
        ),
        "quantity": item.cantidad,
        "notes": item.notas or "",
        "image_url": item.producto.imagen_url if item.producto else None,
        "created_at": item.created_at.isoformat(),
        "wait_label": time_ago_label(item.created_at),
    }


def recent_inventory_movements(limit=20):
    return (
        MovimientoInventario.query.options(
            joinedload(MovimientoInventario.producto),
            joinedload(MovimientoInventario.usuario),
        )
        .order_by(MovimientoInventario.created_at.desc())
        .limit(limit)
        .all()
    )


def recent_cash_movements(limit=15, session_id=None):
    query = MovimientoCaja.query.order_by(MovimientoCaja.created_at.desc())
    if session_id is not None:
        query = query.filter(MovimientoCaja.sesion_caja_id == session_id)
    return query.limit(limit).all()


def session_cash_expected(session_open):
    if session_open is None:
        return ZERO

    apertura = money(session_open.monto_apertura)
    ingresos = ZERO
    egresos = ZERO
    ventas_efectivo = ZERO

    for movimiento in session_open.movimientos:
        if movimiento.tipo == "ingreso":
            ingresos += money(movimiento.monto)
        else:
            egresos += money(movimiento.monto)

    for orden in session_open.ordenes:
        for pago in orden.pagos:
            if pago.metodo == "efectivo":
                ventas_efectivo += money(pago.monto)

    return money(apertura + ingresos + ventas_efectivo - egresos)


def session_card_total(session_open):
    if session_open is None:
        return ZERO

    total = ZERO
    for orden in session_open.ordenes:
        for pago in orden.pagos:
            if pago.metodo == "tarjeta":
                total += money(pago.monto)
    return money(total)


def session_sales_total(session_open):
    if session_open is None:
        return ZERO
    total = ZERO
    for orden in session_open.ordenes:
        for pago in orden.pagos:
            total += money(pago.monto)
    return money(total)


def calculate_order_total(order):
    total = ZERO
    for item in order.items:
        if item.estado == "cancelado":
            continue
        total += item.subtotal
    return money(total)


def sync_order(order):
    order.total = calculate_order_total(order)

    if is_takeout_table(order.mesa):
        order.mesa.estado = "disponible"
        return

    if order.estado in {"pagada", "cancelada"}:
        order.mesa.estado = "disponible"
        if order.items:
            order.mesa.limpieza_estado = "sucia"
        return

    if order.items_activos:
        order.mesa.estado = "ocupada"
    else:
        order.mesa.estado = "disponible"


def clear_divisiones(order):
    for division in list(order.divisiones):
        db.session.delete(division)
    db.session.flush()


def reset_divisiones_if_possible(order):
    if not order.divisiones:
        return False, None

    if any(division.pagada for division in order.divisiones):
        return (
            False,
            "Ya hay personas cobradas en esta división; no puedes modificar los items.",
        )

    clear_divisiones(order)
    return True, "La división de cuenta se reinició porque la orden cambió."


def settle_order(order):
    sync_order(order)

    if order.total > ZERO and order.total_pagado >= order.total and order.todos_entregados:
        order.estado = "pagada"
        if not is_takeout_table(order.mesa):
            order.mesa.estado = "disponible"
            order.mesa.limpieza_estado = "sucia"

        for item in order.items:
            if item.estado == "cancelado" or item.pagado:
                continue

            item.pagado = True

            if item.producto and item.producto.controla_stock:
                item.producto.stock_actual -= item.cantidad
                movimiento = MovimientoInventario(
                    producto_id=item.producto_id,
                    tipo="venta",
                    cantidad_paquetes=None,
                    cantidad_unidades=item.cantidad,
                    precio_unitario=item.costo_unitario or item.producto.precio_costo,
                    notas=f"Venta de orden #{order.id}",
                    usuario_id=order.usuario_id,
                )
                db.session.add(movimiento)
    elif order.estado != "cancelada":
        order.estado = "abierta"
        if order.items_activos and not is_takeout_table(order.mesa):
            order.mesa.estado = "ocupada"


def initial_item_status(producto):
    return "pendiente" if producto.requiere_cocina else "listo"


def item_can_be_prepared(user, item):
    if not user:
        return False
    return item.requiere_cocina and item.estado == "pendiente" and user.rol in {
        "dueño",
        "cocina",
    }


def item_can_be_delivered(user, item):
    if not user:
        return False
    return item.estado == "listo" and user.rol in {"dueño", "mesero"}


def order_can_receive_payment(user, order):
    if not user or user.rol not in {"dueño", "cajero"}:
        return False, "Solo dueño o cajero pueden cobrar órdenes."
    if order.estado != "abierta":
        return False, "La orden ya no está abierta para cobro."
    if not order.items_activos:
        return False, "La orden no tiene items activos."
    if not order.todos_entregados:
        return False, "No se puede cobrar hasta que todos los items estén entregados."
    return True, None


def division_can_receive_payment(user, division):
    if not user or user.rol not in {"dueño", "cajero"}:
        return False, "Solo dueño o cajero pueden cobrar cuentas divididas."
    if division.pagada:
        return False, "Esa persona ya fue cobrada."
    if not division.items:
        return False, "Esa división no tiene items asignados."
    if not division.todos_entregados:
        return False, "Esa cuenta no puede cobrarse hasta que sus items estén entregados."
    return True, None


def build_split_matrix(order, people_count):
    existing = {}
    for division in order.divisiones:
        person = division.numero_persona
        for division_item in division.items:
            existing[(division_item.orden_item_id, person)] = division_item.cantidad

    rows = []
    for item in order.items_activos:
        assignments = {}
        assigned_total = 0
        for person in range(1, people_count + 1):
            qty = existing.get((item.id, person), 0)
            assignments[person] = qty
            assigned_total += qty
        rows.append(
            {
                "item": item,
                "assignments": assignments,
                "assigned_total": assigned_total,
            }
        )
    return rows


def save_split_configuration(order, people_count, labels, assignment_map):
    clear_divisiones(order)

    divisions = []
    for person in range(1, people_count + 1):
        division = OrdenDivision(
            orden=order,
            numero_persona=person,
            etiqueta=labels.get(person) or None,
            total=ZERO,
            pagada=False,
        )
        db.session.add(division)
        divisions.append(division)

    db.session.flush()

    division_by_person = {division.numero_persona: division for division in divisions}

    for item in order.items_activos:
        for person in range(1, people_count + 1):
            qty = assignment_map.get((item.id, person), 0)
            if qty <= 0:
                continue

            subtotal = money(as_decimal(item.precio_unitario) * qty)
            division = division_by_person[person]

            division_item = OrdenDivisionItem(
                division=division,
                orden_item=item,
                cantidad=qty,
                subtotal=subtotal,
            )
            division.total = money(as_decimal(division.total) + subtotal)
            db.session.add(division_item)


def validate_split_assignment(order, people_count, form_data):
    errors = []
    labels = {}
    assignments = {}

    if people_count < 2 or people_count > 10:
        return None, None, ["La cuenta solo puede dividirse entre 2 y 10 personas."]

    for person in range(1, people_count + 1):
        label = (form_data.get(f"label_{person}") or "").strip()
        labels[person] = label

    for item in order.items_activos:
        total_assigned = 0
        for person in range(1, people_count + 1):
            raw_value = form_data.get(f"item_{item.id}_person_{person}", "0")
            qty = parse_int(raw_value, 0)
            if qty < 0:
                errors.append(
                    f"No puedes usar cantidades negativas en {item.producto.nombre}."
                )
            if qty > item.cantidad:
                errors.append(
                    f"No puedes asignar más de {item.cantidad} unidades de {item.producto.nombre} a una persona."
                )
            assignments[(item.id, person)] = qty
            total_assigned += qty

        if total_assigned != item.cantidad:
            errors.append(
                f"Debes repartir exactamente {item.cantidad} unidades de {item.producto.nombre}."
            )

    return labels, assignments, errors
