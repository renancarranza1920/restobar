import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import inspect, text
from sqlalchemy.orm import joinedload, selectinload

from .extensions import db
from .models import (
    AuditLog,
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
    Rol,
    SesionCaja,
    Usuario,
    Zona,
    as_decimal,
)


CENTAVOS = Decimal("0.01")
ZERO = Decimal("0.00")
LOW_STOCK_THRESHOLD = 5
ADMIN_ROLE_CODE = "administrador"
LEGACY_ADMIN_ROLE_CODES = {"due\u00f1o", "due\u00c3\u00b1o", "dueno"}
MODULE_PERMISSION_ALIASES = {
    "dashboard": "dashboard.view",
    "mesas": "mesas.view",
    "zonas": "zonas.view",
    "categorias": "categorias.view",
    "ordenes": "ordenes.view",
    "productos": "productos.view",
    "caja": "caja.view",
    "cocina": "cocina.view",
    "inventario": "inventario.view",
    "usuarios": "usuarios.view",
    "reportes": "reportes.view",
    "auditoria": "auditoria.view",
}

FEATURE_DEFINITIONS = [
    {
        "key": "dashboard",
        "label": "Inicio",
        "description": "Ver resumen de ventas, métricas y alertas.",
        "group": "General",
    },
    {
        "key": "mesas",
        "label": "Mesas",
        "description": "Ver mesas, abrir órdenes y administrar limpieza.",
        "group": "Operación",
    },
    {
        "key": "zonas",
        "label": "Zonas",
        "description": "Crear, editar y eliminar zonas del restaurante.",
        "group": "Configuración",
    },
    {
        "key": "categorias",
        "label": "Categorías",
        "description": "Organizar categorías y definir si pasan por cocina.",
        "group": "Catálogo",
    },
    {
        "key": "ordenes",
        "label": "Órdenes",
        "description": "Ver órdenes, agregar productos y manejar items.",
        "group": "Operación",
    },
    {
        "key": "productos",
        "label": "Productos",
        "description": "Crear, editar, agotar y eliminar productos.",
        "group": "Catálogo",
    },
    {
        "key": "caja",
        "label": "Caja",
        "description": "Abrir/cerrar caja, cobrar órdenes y registrar movimientos.",
        "group": "Finanzas",
    },
    {
        "key": "cocina",
        "label": "Cocina",
        "description": "Ver comandas y marcar productos de cocina como listos.",
        "group": "Operación",
    },
    {
        "key": "inventario",
        "label": "Inventario",
        "description": "Ver stock y registrar compras, ventas o ajustes.",
        "group": "Inventario",
    },
    {
        "key": "usuarios",
        "label": "Usuarios y roles",
        "description": "Administrar usuarios, roles y permisos del sistema.",
        "group": "Seguridad",
    },
    {
        "key": "reportes",
        "label": "Reportes",
        "description": "Consultar y exportar reportes operativos y financieros.",
        "group": "Finanzas",
    },
]

PERMISSION_DEFINITIONS = [
    {"key": "dashboard.view", "label": "Ver inicio", "description": "Ver el panel inicial adaptado al rol.", "group": "General"},
    {"key": "mesas.view", "label": "Ver mesas", "description": "Consultar mesas, zonas ocupadas y limpieza.", "group": "Mesas"},
    {"key": "mesas.create", "label": "Crear mesas", "description": "Registrar nuevas mesas.", "group": "Mesas"},
    {"key": "mesas.edit", "label": "Editar mesas", "description": "Cambiar datos y estado de limpieza.", "group": "Mesas"},
    {"key": "mesas.delete", "label": "Eliminar mesas", "description": "Borrar mesas sin historial.", "group": "Mesas"},
    {"key": "zonas.view", "label": "Ver zonas", "description": "Consultar zonas del restaurante.", "group": "Configuracion"},
    {"key": "zonas.create", "label": "Crear zonas", "description": "Registrar nuevas zonas.", "group": "Configuracion"},
    {"key": "zonas.edit", "label": "Editar zonas", "description": "Actualizar nombres de zonas.", "group": "Configuracion"},
    {"key": "zonas.delete", "label": "Eliminar zonas", "description": "Borrar zonas sin mesas asociadas.", "group": "Configuracion"},
    {"key": "categorias.view", "label": "Ver categorias", "description": "Consultar grupos de productos.", "group": "Catalogo"},
    {"key": "categorias.create", "label": "Crear categorias", "description": "Registrar nuevas categorias.", "group": "Catalogo"},
    {"key": "categorias.edit", "label": "Editar categorias", "description": "Actualizar categorias y cocina.", "group": "Catalogo"},
    {"key": "categorias.delete", "label": "Eliminar categorias", "description": "Borrar categorias sin productos.", "group": "Catalogo"},
    {"key": "productos.view", "label": "Ver productos", "description": "Consultar catalogo, precios y disponibilidad.", "group": "Catalogo"},
    {"key": "productos.create", "label": "Crear productos", "description": "Registrar nuevos productos.", "group": "Catalogo"},
    {"key": "productos.edit", "label": "Editar productos", "description": "Actualizar datos, precios e imagenes.", "group": "Catalogo"},
    {"key": "productos.availability", "label": "Cambiar disponibilidad", "description": "Marcar productos como disponibles o agotados.", "group": "Catalogo"},
    {"key": "productos.delete", "label": "Eliminar productos", "description": "Borrar productos sin historial.", "group": "Catalogo"},
    {"key": "ordenes.view", "label": "Ver ordenes", "description": "Consultar ordenes y detalle.", "group": "Ordenes"},
    {"key": "ordenes.create", "label": "Crear ordenes", "description": "Abrir ordenes de mesa o para llevar.", "group": "Ordenes"},
    {"key": "ordenes.items", "label": "Agregar productos", "description": "Agregar items a ordenes abiertas.", "group": "Ordenes"},
    {"key": "ordenes.deliver", "label": "Entregar items", "description": "Marcar productos listos como entregados.", "group": "Ordenes"},
    {"key": "ordenes.cancel_item", "label": "Cancelar items", "description": "Cancelar items no cobrados.", "group": "Ordenes"},
    {"key": "ordenes.ticket", "label": "Ver tickets", "description": "Abrir tickets de venta o cocina.", "group": "Ordenes"},
    {"key": "caja.view", "label": "Ver caja", "description": "Consultar caja, sesiones y movimientos.", "group": "Caja"},
    {"key": "caja.open", "label": "Abrir caja", "description": "Iniciar sesion de caja.", "group": "Caja"},
    {"key": "caja.close", "label": "Cerrar caja", "description": "Cerrar sesion de caja.", "group": "Caja"},
    {"key": "caja.movements", "label": "Movimientos de caja", "description": "Registrar ingresos y egresos manuales.", "group": "Caja"},
    {"key": "caja.charge", "label": "Cobrar ordenes", "description": "Registrar pagos y cuentas divididas.", "group": "Caja"},
    {"key": "caja.cancel_order", "label": "Cancelar ordenes", "description": "Cancelar ordenes abiertas sin pagos.", "group": "Caja"},
    {"key": "cocina.view", "label": "Ver cocina", "description": "Consultar comandas pendientes.", "group": "Cocina"},
    {"key": "cocina.prepare", "label": "Preparar comandas", "description": "Marcar items de cocina como listos/entregados.", "group": "Cocina"},
    {"key": "inventario.view", "label": "Ver inventario", "description": "Consultar stock y movimientos.", "group": "Inventario"},
    {"key": "inventario.create", "label": "Registrar movimientos", "description": "Crear compras, ventas y ajustes.", "group": "Inventario"},
    {"key": "reportes.view", "label": "Ver reportes", "description": "Consultar reportes operativos y financieros.", "group": "Reportes"},
    {"key": "reportes.export", "label": "Exportar reportes", "description": "Descargar CSV y PDF.", "group": "Reportes"},
    {"key": "usuarios.view", "label": "Ver usuarios", "description": "Consultar cuentas y roles.", "group": "Seguridad"},
    {"key": "usuarios.create", "label": "Crear usuarios", "description": "Registrar cuentas nuevas.", "group": "Seguridad"},
    {"key": "usuarios.edit", "label": "Editar usuarios", "description": "Actualizar datos, roles y estado.", "group": "Seguridad"},
    {"key": "usuarios.delete", "label": "Eliminar usuarios", "description": "Eliminar cuentas sin movimientos.", "group": "Seguridad"},
    {"key": "usuarios.reset_password", "label": "Resetear contrasenas", "description": "Cambiar contrasenas de otros usuarios.", "group": "Seguridad"},
    {"key": "roles.view", "label": "Ver roles", "description": "Consultar roles y permisos.", "group": "Seguridad"},
    {"key": "roles.create", "label": "Crear roles", "description": "Crear roles personalizados.", "group": "Seguridad"},
    {"key": "roles.edit", "label": "Editar roles", "description": "Modificar permisos de roles.", "group": "Seguridad"},
    {"key": "roles.delete", "label": "Eliminar roles", "description": "Eliminar roles sin usuarios.", "group": "Seguridad"},
    {"key": "security.change_password", "label": "Cambiar contrasena propia", "description": "Actualizar la contrasena de la cuenta actual.", "group": "Seguridad"},
    {"key": "auditoria.view", "label": "Ver auditoria", "description": "Consultar cambios importantes del sistema.", "group": "Auditoria"},
]

ALL_FEATURE_KEYS = {feature["key"] for feature in FEATURE_DEFINITIONS}
ALL_PERMISSION_KEYS = {permission["key"] for permission in PERMISSION_DEFINITIONS}

DEFAULT_ROLE_DETAILS = {
    ADMIN_ROLE_CODE: {
        "nombre": "Administrador",
        "descripcion": "Acceso completo a todas las areas del sistema.",
    },
}

DEFAULT_FEATURES_BY_ROLE = {
    ADMIN_ROLE_CODE: set(ALL_PERMISSION_KEYS),
}

FEATURES_BY_ROLE = DEFAULT_FEATURES_BY_ROLE

NAV_ITEMS = [
    {
        "feature": "dashboard",
        "label": "Inicio",
        "icon": "fa-house",
        "endpoint": "web.dashboard",
        "active_endpoints": {"web.dashboard"},
    },
    {
        "feature": "mesas",
        "label": "Mesas",
        "icon": "fa-chair",
        "endpoint": "web.mesas",
        "active_endpoints": {"web.mesas", "web.nueva_mesa", "web.editar_mesa"},
    },
    {
        "feature": "zonas",
        "label": "Zonas",
        "icon": "fa-layer-group",
        "endpoint": "web.zonas",
        "active_endpoints": {"web.zonas", "web.nueva_zona", "web.editar_zona"},
    },
    {
        "feature": "categorias",
        "label": "Categorias",
        "icon": "fa-tags",
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
        "icon": "fa-receipt",
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
        "icon": "fa-box-open",
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
        "icon": "fa-cash-register",
        "endpoint": "web.caja",
        "active_endpoints": {"web.caja"},
    },
    {
        "feature": "reportes",
        "label": "Reportes",
        "icon": "fa-chart-line",
        "endpoint": "web.reportes",
        "active_endpoints": {"web.reportes", "web.exportar_reporte"},
    },
    {
        "feature": "cocina",
        "label": "Cocina",
        "icon": "fa-fire-burner",
        "endpoint": "web.cocina",
        "active_endpoints": {"web.cocina"},
    },
    {
        "feature": "inventario",
        "label": "Inventario",
        "icon": "fa-boxes-stacked",
        "endpoint": "web.inventario",
        "active_endpoints": {"web.inventario", "web.nuevo_movimiento_inventario"},
    },
    {
        "feature": "usuarios",
        "label": "Usuarios",
        "icon": "fa-users-gear",
        "endpoint": "web.usuarios",
        "active_endpoints": {
            "web.usuarios",
            "web.nuevo_usuario",
            "web.editar_usuario",
            "web.roles",
            "web.nuevo_rol",
            "web.crear_rol",
            "web.editar_rol",
            "web.actualizar_rol",
            "web.eliminar_rol",
        },
    },
    {
        "feature": "auditoria",
        "label": "Auditoria",
        "icon": "fa-clock-rotate-left",
        "endpoint": "web.auditoria",
        "active_endpoints": {"web.auditoria"},
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
    if role in LEGACY_ADMIN_ROLE_CODES:
        return DEFAULT_ROLE_DETAILS[ADMIN_ROLE_CODE]["nombre"]
    rol = get_role(role)
    if rol:
        return rol.nombre
    details = DEFAULT_ROLE_DETAILS.get(role, {})
    return details.get("nombre", role)


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


def feature_definitions():
    return FEATURE_DEFINITIONS


def permission_definitions():
    return PERMISSION_DEFINITIONS


def normalize_permission_key(permission):
    return MODULE_PERMISSION_ALIASES.get(permission, permission)


def expand_legacy_permissions(values):
    expanded = set()
    for value in values or []:
        value = str(value).strip()
        if not value:
            continue
        if value in MODULE_PERMISSION_ALIASES:
            prefix = f"{value}."
            expanded.update(
                permission["key"]
                for permission in PERMISSION_DEFINITIONS
                if permission["key"].startswith(prefix)
            )
        elif value in ALL_PERMISSION_KEYS:
            expanded.add(value)
    return expanded


def role_table_exists():
    try:
        return inspect(db.engine).has_table("roles")
    except Exception:
        return False


def get_roles():
    if role_table_exists():
        roles = Rol.query.order_by(Rol.nombre.asc()).all()
        if roles:
            return roles

    fallback_roles = []
    for code, details in DEFAULT_ROLE_DETAILS.items():
        rol = Rol(codigo=code, nombre=details["nombre"], descripcion=details["descripcion"])
        rol.permisos = expand_legacy_permissions(DEFAULT_FEATURES_BY_ROLE.get(code, set()))
        fallback_roles.append(rol)
    return fallback_roles


def get_role(role_code):
    if not role_code:
        return None
    if role_table_exists():
        rol = db.session.get(Rol, role_code)
        if rol:
            return rol
    details = DEFAULT_ROLE_DETAILS.get(role_code)
    if not details:
        return None
    rol = Rol(codigo=role_code, nombre=details["nombre"], descripcion=details["descripcion"])
    rol.permisos = expand_legacy_permissions(DEFAULT_FEATURES_BY_ROLE.get(role_code, set()))
    return rol


def valid_role_code(role_code):
    return get_role(role_code) is not None


def permissions_for_role(role_code):
    if role_code in LEGACY_ADMIN_ROLE_CODES:
        return DEFAULT_FEATURES_BY_ROLE[ADMIN_ROLE_CODE]
    rol = get_role(role_code)
    if rol:
        return expand_legacy_permissions(rol.permisos)
    return expand_legacy_permissions(DEFAULT_FEATURES_BY_ROLE.get(role_code, set()))


def user_can(user, feature):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    permissions = permissions_for_role(user.rol)
    permission_key = normalize_permission_key(feature)
    if permission_key in permissions:
        return True
    if feature in MODULE_PERMISSION_ALIASES:
        return any(permission.startswith(f"{feature}.") for permission in permissions)
    return False


def navigation_for_user(user):
    return [item for item in NAV_ITEMS if user_can(user, item["feature"])]


def default_endpoint_for_user(user):
    if not user:
        return "web.login"
    preferred_endpoints = [
        ("dashboard", "web.dashboard"),
        ("ordenes", "web.ordenes"),
        ("mesas", "web.mesas"),
        ("caja", "web.caja"),
        ("cocina", "web.cocina"),
        ("productos", "web.productos"),
        ("inventario", "web.inventario"),
        ("reportes", "web.reportes"),
        ("usuarios", "web.usuarios"),
    ]
    for feature, endpoint in preferred_endpoints:
        if user_can(user, feature):
            return endpoint
    return "web.login"


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
            rol=ADMIN_ROLE_CODE,
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


def bootstrap_roles_permissions():
    inspector = inspect(db.engine)
    if not inspector.has_table("roles"):
        Rol.__table__.create(db.engine)

    changed = False
    legacy_roles = Rol.query.filter(Rol.codigo.in_(LEGACY_ADMIN_ROLE_CODES)).all()
    for legacy_role in legacy_roles:
        db.session.delete(legacy_role)
        changed = True

    for code, details in DEFAULT_ROLE_DETAILS.items():
        rol = db.session.get(Rol, code)
        if rol is None:
            rol = Rol(
                codigo=code,
                nombre=details["nombre"],
                descripcion=details["descripcion"],
            )
            rol.permisos = DEFAULT_FEATURES_BY_ROLE.get(code, set())
            db.session.add(rol)
            changed = True
        else:
            if not rol.nombre:
                rol.nombre = details["nombre"]
                changed = True
            if rol.permisos_csv is None:
                rol.permisos = DEFAULT_FEATURES_BY_ROLE.get(code, set())
                changed = True
            expanded_permissions = expand_legacy_permissions(rol.permisos)
            target_permissions = (
                DEFAULT_FEATURES_BY_ROLE[ADMIN_ROLE_CODE]
                if code == ADMIN_ROLE_CODE
                else expanded_permissions
            )
            if target_permissions != rol.permisos:
                rol.permisos = target_permissions
                changed = True

    seeded_role_codes = {"cajero", "mesero", "cocina"}
    seeded_roles = Rol.query.filter(Rol.codigo.in_(seeded_role_codes)).all()
    for seeded_role in seeded_roles:
        db.session.delete(seeded_role)
        changed = True

    if changed:
        db.session.commit()


def bootstrap_security_schema():
    inspector = inspect(db.engine)
    if inspector.has_table("usuarios"):
        user_columns = {column["name"] for column in inspector.get_columns("usuarios")}
        if "must_change_password" not in user_columns:
            db.session.execute(
                text("ALTER TABLE usuarios ADD COLUMN must_change_password BOOLEAN DEFAULT FALSE NOT NULL")
            )
            db.session.commit()

    if inspector.has_table("usuarios") and not inspector.has_table("audit_logs"):
        AuditLog.__table__.create(db.engine)


def audit_event(action, entity, entity_id=None, summary=None, details=None, commit=False):
    try:
        user_id = current_user.id if getattr(current_user, "is_authenticated", False) else None
    except Exception:
        user_id = None

    request_details = {}
    try:
        request_details = {
            "path": request.path,
            "method": request.method,
        }
        ip_address = request.headers.get("X-Forwarded-For", request.remote_addr)
        user_agent = (request.user_agent.string or "")[:255]
    except RuntimeError:
        ip_address = None
        user_agent = None

    payload = details or {}
    if request_details:
        payload = {**payload, "request": request_details}

    log = AuditLog(
        usuario_id=user_id,
        accion=action,
        entidad=entity,
        entidad_id=str(entity_id) if entity_id is not None else None,
        resumen=summary,
        detalles_json=json.dumps(payload, ensure_ascii=False, default=str) if payload else None,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.session.add(log)
    if commit:
        db.session.commit()
    return log


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
        .filter_by(limpieza_estado="limpia")
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


def mesa_has_other_active_orders(mesa_id, excluded_order_id=None):
    if not mesa_id:
        return False

    query = Orden.query.filter_by(mesa_id=mesa_id, estado="abierta")
    if excluded_order_id:
        query = query.filter(Orden.id != excluded_order_id)
    return db.session.query(query.exists()).scalar()


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
        .order_by(Orden.created_at.desc())
        .all()
    )
    active_orders_by_table = {}
    for order in active_orders:
        active_orders_by_table.setdefault(order.mesa_id, []).append(order)
    return zonas, active_orders_by_table


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
    normalize_item_delivery_states(order)
    order.total = calculate_order_total(order)

    if is_takeout_table(order.mesa):
        order.mesa.estado = "disponible"
        return

    if order.estado in {"pagada", "cancelada"}:
        if mesa_has_other_active_orders(order.mesa_id, order.id):
            order.mesa.estado = "ocupada"
        else:
            order.mesa.estado = "disponible"
        if order.items and order.mesa.estado != "ocupada":
            order.mesa.limpieza_estado = "sucia"
        return

    if order.items_activos:
        order.mesa.estado = "ocupada"
    elif mesa_has_other_active_orders(order.mesa_id, order.id):
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


def normalize_item_delivery_states(order):
    changed = False
    for item in order.items_activos:
        if item.estado == "listo" or (
            not item.requiere_cocina and item.estado == "pendiente"
        ):
            item.estado = "entregado"
            changed = True
    return changed


def settle_order(order):
    sync_order(order)

    if order.total > ZERO and order.total_pagado >= order.total and order.todos_entregados:
        order.estado = "pagada"
        if not is_takeout_table(order.mesa):
            if mesa_has_other_active_orders(order.mesa_id, order.id):
                order.mesa.estado = "ocupada"
            else:
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
    return "pendiente" if producto.requiere_cocina else "entregado"


def normalize_item_notes(notes):
    cleaned = (notes or "").strip()
    return cleaned or None


def item_merge_key(item):
    return (
        item.producto_id,
        normalize_item_notes(item.notas),
        money(item.precio_unitario),
        money(item.costo_unitario),
        item.estado,
        bool(item.pagado),
    )


def can_merge_order_item(item):
    return (
        item.estado != "cancelado"
        and not item.pagado
        and not item.division_items
    )


def consolidate_order_items(order):
    if order.divisiones:
        return False

    seen = {}
    changed = False
    for item in list(order.items):
        if not can_merge_order_item(item):
            continue

        key = item_merge_key(item)
        existing = seen.get(key)
        if existing is None:
            seen[key] = item
            continue

        existing.cantidad += item.cantidad
        db.session.delete(item)
        changed = True

    if changed:
        db.session.flush()
        sync_order(order)
    return changed


def add_or_increment_order_item(order, product, quantity, notes=None):
    notes = normalize_item_notes(notes)
    status = initial_item_status(product)
    key = (
        product.id,
        notes,
        money(product.precio_venta),
        money(product.precio_costo),
        status,
        False,
    )

    for item in order.items:
        if can_merge_order_item(item) and item_merge_key(item) == key:
            item.cantidad += quantity
            return item, True

    item = OrdenItem(
        orden=order,
        producto=product,
        cantidad=quantity,
        precio_unitario=product.precio_venta,
        costo_unitario=product.precio_costo,
        notas=notes,
        estado=status,
    )
    db.session.add(item)
    return item, False


def item_can_be_prepared(user, item):
    if not user:
        return False
    return item.requiere_cocina and item.estado == "pendiente" and user_can(user, "cocina.prepare")


def item_can_be_delivered(user, item):
    if not user:
        return False
    return item.requiere_cocina and item.estado == "listo" and user_can(user, "ordenes.deliver")


def order_can_receive_payment(user, order):
    if not user or not user_can(user, "caja.charge"):
        return False, "No tienes permiso de caja para cobrar órdenes."
    if order.estado != "abierta":
        return False, "La orden ya no está abierta para cobro."
    if not order.items_activos:
        return False, "La orden no tiene items activos."
    if not order.todos_entregados:
        return False, "No se puede cobrar hasta que todos los items estén entregados."
    return True, None


def division_can_receive_payment(user, division):
    if not user or not user_can(user, "caja.charge"):
        return False, "No tienes permiso de caja para cobrar cuentas divididas."
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
