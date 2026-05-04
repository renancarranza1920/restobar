import hmac
import json
from datetime import datetime
from decimal import Decimal

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def as_decimal(value):
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def as_float(value):
    return float(as_decimal(value))


class Usuario(UserMixin, db.Model):
    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True)
    nickname = db.Column(db.String(50), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    apellido = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    rol = db.Column(db.String(30), nullable=False)
    activo = db.Column(db.Boolean, default=True, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    sesiones_caja = db.relationship("SesionCaja", back_populates="usuario", lazy=True)
    ordenes = db.relationship("Orden", back_populates="usuario", lazy=True)
    movimientos_inventario = db.relationship(
        "MovimientoInventario", back_populates="usuario", lazy=True
    )
    lista_espera = db.relationship("ListaEspera", back_populates="usuario", lazy=True)

    @property
    def nombre_completo(self):
        return f"{self.nombre} {self.apellido}".strip()

    @property
    def is_active(self):
        return self.activo

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    @property
    def uses_legacy_plaintext_password(self):
        stored_password = self.password_hash or ""
        return bool(stored_password) and not stored_password.startswith(
            ("pbkdf2:", "scrypt:")
        )

    def check_password(self, password):
        stored_password = self.password_hash or ""
        if not stored_password:
            return False
        if self.uses_legacy_plaintext_password:
            return hmac.compare_digest(stored_password, password)
        return check_password_hash(stored_password, password)

    def to_dict(self):
        return {
            "id": self.id,
            "nickname": self.nickname,
            "nombre": self.nombre,
            "apellido": self.apellido,
            "nombre_completo": self.nombre_completo,
            "rol": self.rol,
            "activo": self.activo,
            "must_change_password": self.must_change_password,
        }

    def __repr__(self):
        return f"<Usuario {self.nickname}>"


class Rol(db.Model):
    __tablename__ = "roles"

    codigo = db.Column(db.String(30), primary_key=True)
    nombre = db.Column(db.String(80), nullable=False)
    descripcion = db.Column(db.String(255))
    permisos_csv = db.Column("permisos", db.Text, default="", nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    @property
    def permisos(self):
        return {
            permiso.strip()
            for permiso in (self.permisos_csv or "").split(",")
            if permiso.strip()
        }

    @permisos.setter
    def permisos(self, values):
        cleaned = sorted({str(value).strip() for value in values if str(value).strip()})
        self.permisos_csv = ",".join(cleaned)

    def to_dict(self):
        return {
            "codigo": self.codigo,
            "nombre": self.nombre,
            "descripcion": self.descripcion,
            "permisos": sorted(self.permisos),
        }


class PreferenciaSistema(db.Model):
    __tablename__ = "preferencias_sistema"

    clave = db.Column(db.String(80), primary_key=True)
    valor = db.Column(db.Text)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self):
        return {"clave": self.clave, "valor": self.valor}


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=True)
    accion = db.Column(db.String(80), nullable=False)
    entidad = db.Column(db.String(80), nullable=False)
    entidad_id = db.Column(db.String(80), nullable=True)
    resumen = db.Column(db.String(255), nullable=True)
    detalles_json = db.Column("detalles", db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    usuario = db.relationship("Usuario", lazy=True)

    @property
    def detalles(self):
        if not self.detalles_json:
            return {}
        try:
            return json.loads(self.detalles_json)
        except ValueError:
            return {}

    @detalles.setter
    def detalles(self, value):
        self.detalles_json = json.dumps(value or {}, ensure_ascii=False, default=str)


class Zona(db.Model):
    __tablename__ = "zonas"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(50), nullable=False)

    mesas = db.relationship("Mesa", back_populates="zona", lazy=True)

    def to_dict(self):
        return {"id": self.id, "nombre": self.nombre}


class Mesa(db.Model):
    __tablename__ = "mesas"

    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.Integer, nullable=False)
    nombre_alias = db.Column(db.String(50))
    zona_id = db.Column(db.Integer, db.ForeignKey("zonas.id"), nullable=False)
    estado = db.Column(
        db.Enum("disponible", "ocupada", name="mesas_estado"),
        default="disponible",
        nullable=False,
    )
    limpieza_estado = db.Column(
        db.Enum("limpia", "sucia", name="mesas_limpieza_estado"),
        default="limpia",
        nullable=False,
    )

    zona = db.relationship("Zona", back_populates="mesas", lazy=True)
    ordenes = db.relationship("Orden", back_populates="mesa", lazy=True)
    lista_espera = db.relationship("ListaEspera", back_populates="mesa", lazy=True)

    @property
    def etiqueta(self):
        return self.nombre_alias or f"Mesa {self.numero}"

    def to_dict(self):
        return {
            "id": self.id,
            "numero": self.numero,
            "nombre_alias": self.nombre_alias,
            "etiqueta": self.etiqueta,
            "estado": self.estado,
            "limpieza_estado": self.limpieza_estado,
            "zona_id": self.zona_id,
            "zona": self.zona.nombre if self.zona else None,
        }


class ListaEspera(db.Model):
    __tablename__ = "lista_espera"

    id = db.Column(db.Integer, primary_key=True)
    nombre_cliente = db.Column(db.String(100), nullable=False)
    personas = db.Column(db.Integer, default=1, nullable=False)
    telefono = db.Column(db.String(40))
    notas = db.Column(db.String(200))
    estado = db.Column(
        db.Enum("esperando", "sentado", "cancelado", name="lista_espera_estado"),
        default="esperando",
        nullable=False,
    )
    mesa_id = db.Column(db.Integer, db.ForeignKey("mesas.id"), nullable=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime)

    mesa = db.relationship("Mesa", back_populates="lista_espera", lazy=True)
    usuario = db.relationship("Usuario", back_populates="lista_espera", lazy=True)

    @property
    def etiqueta_personas(self):
        return f"{self.personas} persona{'' if self.personas == 1 else 's'}"

    def to_dict(self):
        return {
            "id": self.id,
            "nombre_cliente": self.nombre_cliente,
            "personas": self.personas,
            "telefono": self.telefono,
            "notas": self.notas,
            "estado": self.estado,
            "mesa_id": self.mesa_id,
            "mesa": self.mesa.etiqueta if self.mesa else None,
            "usuario": self.usuario.nombre_completo if self.usuario else None,
            "created_at": self.created_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


class Categoria(db.Model):
    __tablename__ = "categorias"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(50), nullable=False)
    envia_a_cocina = db.Column(db.Boolean, default=False, nullable=False)

    productos = db.relationship("Producto", back_populates="categoria", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "envia_a_cocina": self.envia_a_cocina,
        }


class Producto(db.Model):
    __tablename__ = "productos"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    imagen_url = db.Column(db.String(255))
    categoria_id = db.Column(db.Integer, db.ForeignKey("categorias.id"), nullable=False)
    precio_costo = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    precio_venta = db.Column(db.Numeric(10, 2), nullable=False)
    unidad_compra = db.Column(db.String(30), default="unidad", nullable=False)
    unidades_por_paquete = db.Column(db.Integer, default=1, nullable=False)
    stock_actual = db.Column(db.Integer, default=0, nullable=False)
    maneja_stock = db.Column(db.Boolean, default=True, nullable=False)
    disponible = db.Column(db.Boolean, default=True, nullable=False)

    categoria = db.relationship("Categoria", back_populates="productos", lazy=True)
    orden_items = db.relationship("OrdenItem", back_populates="producto", lazy=True)
    movimientos_inventario = db.relationship(
        "MovimientoInventario", back_populates="producto", lazy=True
    )

    @property
    def requiere_cocina(self):
        return bool(self.categoria and self.categoria.envia_a_cocina)

    @property
    def controla_stock(self):
        return self.maneja_stock and not self.requiere_cocina

    @property
    def stock_bajo(self):
        return self.controla_stock and self.stock_actual <= 12

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "imagen_url": self.imagen_url,
            "categoria_id": self.categoria_id,
            "categoria": self.categoria.nombre if self.categoria else None,
            "precio_costo": as_float(self.precio_costo),
            "precio_venta": as_float(self.precio_venta),
            "unidad_compra": self.unidad_compra,
            "unidades_por_paquete": self.unidades_por_paquete,
            "stock_actual": self.stock_actual,
            "maneja_stock": self.controla_stock,
            "controla_stock": self.controla_stock,
            "disponible": self.disponible,
            "requiere_cocina": self.requiere_cocina,
            "stock_bajo": self.stock_bajo,
        }


class SesionCaja(db.Model):
    __tablename__ = "sesiones_caja"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    fecha_apertura = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    fecha_cierre = db.Column(db.DateTime)
    monto_apertura = db.Column(db.Numeric(10, 2), nullable=False)
    monto_cierre_real = db.Column(db.Numeric(10, 2))
    estado = db.Column(
        db.Enum("abierta", "cerrada", name="sesiones_caja_estado"),
        default="abierta",
        nullable=False,
    )

    usuario = db.relationship("Usuario", back_populates="sesiones_caja", lazy=True)
    movimientos = db.relationship(
        "MovimientoCaja",
        back_populates="sesion_caja",
        lazy=True,
        cascade="all, delete-orphan",
    )
    ordenes = db.relationship("Orden", back_populates="sesion_caja", lazy=True)


class MovimientoCaja(db.Model):
    __tablename__ = "movimientos_caja"

    id = db.Column(db.Integer, primary_key=True)
    sesion_caja_id = db.Column(
        db.Integer, db.ForeignKey("sesiones_caja.id"), nullable=False
    )
    tipo = db.Column(
        db.Enum("ingreso", "egreso", name="movimientos_caja_tipo"), nullable=False
    )
    concepto = db.Column(db.String(200), nullable=False)
    monto = db.Column(db.Numeric(10, 2), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    sesion_caja = db.relationship("SesionCaja", back_populates="movimientos", lazy=True)


class Orden(db.Model):
    __tablename__ = "ordenes"

    id = db.Column(db.Integer, primary_key=True)
    mesa_id = db.Column(db.Integer, db.ForeignKey("mesas.id"), nullable=False)
    sesion_caja_id = db.Column(
        db.Integer, db.ForeignKey("sesiones_caja.id"), nullable=False
    )
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    nombre_cliente = db.Column(db.String(100))
    total = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    estado = db.Column(
        db.Enum("abierta", "pagada", "cancelada", name="ordenes_estado"),
        default="abierta",
        nullable=False,
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    mesa = db.relationship("Mesa", back_populates="ordenes", lazy=True)
    sesion_caja = db.relationship("SesionCaja", back_populates="ordenes", lazy=True)
    usuario = db.relationship("Usuario", back_populates="ordenes", lazy=True)
    items = db.relationship(
        "OrdenItem",
        back_populates="orden",
        lazy=True,
        cascade="all, delete-orphan",
    )
    pagos = db.relationship(
        "Pago",
        back_populates="orden",
        lazy=True,
        cascade="all, delete-orphan",
    )
    divisiones = db.relationship(
        "OrdenDivision",
        back_populates="orden",
        lazy=True,
        cascade="all, delete-orphan",
    )

    @property
    def total_pagado(self):
        total = Decimal("0.00")
        for pago in self.pagos:
            total += as_decimal(pago.monto)
        return total

    @property
    def saldo_pendiente(self):
        saldo = as_decimal(self.total) - self.total_pagado
        return saldo if saldo > Decimal("0.00") else Decimal("0.00")

    @property
    def items_activos(self):
        return [item for item in self.items if item.estado != "cancelado"]

    @property
    def items_entregados(self):
        return [item for item in self.items_activos if item.estado == "entregado"]

    @property
    def todos_entregados(self):
        activos = self.items_activos
        return bool(activos) and all(item.estado == "entregado" for item in activos)

    def to_dict(self):
        return {
            "id": self.id,
            "mesa_id": self.mesa_id,
            "mesa": self.mesa.etiqueta if self.mesa else None,
            "nombre_cliente": self.nombre_cliente,
            "total": as_float(self.total),
            "total_pagado": as_float(self.total_pagado),
            "saldo_pendiente": as_float(self.saldo_pendiente),
            "estado": self.estado,
            "todos_entregados": self.todos_entregados,
            "created_at": self.created_at.isoformat(),
        }


class OrdenItem(db.Model):
    __tablename__ = "orden_items"

    id = db.Column(db.Integer, primary_key=True)
    orden_id = db.Column(db.Integer, db.ForeignKey("ordenes.id"), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey("productos.id"), nullable=False)
    cantidad = db.Column(db.Integer, default=1, nullable=False)
    precio_unitario = db.Column(db.Numeric(10, 2), nullable=False)
    costo_unitario = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    notas = db.Column(db.String(200))
    estado = db.Column(
        db.Enum(
            "pendiente",
            "listo",
            "entregado",
            "cancelado",
            name="orden_items_estado",
        ),
        default="pendiente",
        nullable=False,
    )
    pagado = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    orden = db.relationship("Orden", back_populates="items", lazy=True)
    producto = db.relationship("Producto", back_populates="orden_items", lazy=True)
    division_items = db.relationship(
        "OrdenDivisionItem",
        back_populates="orden_item",
        lazy=True,
        cascade="all, delete-orphan",
    )

    @property
    def subtotal(self):
        return as_decimal(self.precio_unitario) * self.cantidad

    @property
    def requiere_cocina(self):
        return bool(self.producto and self.producto.requiere_cocina)

    def to_dict(self):
        return {
            "id": self.id,
            "orden_id": self.orden_id,
            "producto_id": self.producto_id,
            "producto": self.producto.nombre if self.producto else None,
            "imagen_url": self.producto.imagen_url if self.producto else None,
            "cantidad": self.cantidad,
            "precio_unitario": as_float(self.precio_unitario),
            "subtotal": as_float(self.subtotal),
            "estado": self.estado,
            "pagado": self.pagado,
            "notas": self.notas,
            "requiere_cocina": self.requiere_cocina,
        }


class Pago(db.Model):
    __tablename__ = "pagos"

    id = db.Column(db.Integer, primary_key=True)
    orden_id = db.Column(db.Integer, db.ForeignKey("ordenes.id"), nullable=False)
    metodo = db.Column(
        db.Enum("efectivo", "tarjeta", name="pagos_metodo"), nullable=False
    )
    monto = db.Column(db.Numeric(10, 2), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    orden = db.relationship("Orden", back_populates="pagos", lazy=True)


class MovimientoInventario(db.Model):
    __tablename__ = "movimientos_inventario"

    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey("productos.id"), nullable=False)
    tipo = db.Column(
        db.Enum("compra", "venta", "ajuste", name="movimientos_inventario_tipo"),
        nullable=False,
    )
    cantidad_paquetes = db.Column(db.Integer)
    cantidad_unidades = db.Column(db.Integer, nullable=False)
    precio_unitario = db.Column(db.Numeric(10, 2))
    notas = db.Column(db.String(200))
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    producto = db.relationship(
        "Producto", back_populates="movimientos_inventario", lazy=True
    )
    usuario = db.relationship(
        "Usuario", back_populates="movimientos_inventario", lazy=True
    )


class OrdenDivision(db.Model):
    __tablename__ = "orden_divisiones"

    id = db.Column(db.Integer, primary_key=True)
    orden_id = db.Column(db.Integer, db.ForeignKey("ordenes.id"), nullable=False)
    numero_persona = db.Column(db.Integer, nullable=False)
    etiqueta = db.Column(db.String(50))
    total = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    pagada = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    orden = db.relationship("Orden", back_populates="divisiones", lazy=True)
    items = db.relationship(
        "OrdenDivisionItem",
        back_populates="division",
        lazy=True,
        cascade="all, delete-orphan",
    )

    @property
    def nombre_visible(self):
        return self.etiqueta or f"Persona {self.numero_persona}"

    @property
    def todos_entregados(self):
        return bool(self.items) and all(
            division_item.orden_item.estado == "entregado"
            for division_item in self.items
        )


class OrdenDivisionItem(db.Model):
    __tablename__ = "orden_division_items"

    id = db.Column(db.Integer, primary_key=True)
    division_id = db.Column(
        db.Integer, db.ForeignKey("orden_divisiones.id"), nullable=False
    )
    orden_item_id = db.Column(
        db.Integer, db.ForeignKey("orden_items.id"), nullable=False
    )
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    subtotal = db.Column(db.Numeric(10, 2), default=0, nullable=False)

    division = db.relationship("OrdenDivision", back_populates="items", lazy=True)
    orden_item = db.relationship("OrdenItem", back_populates="division_items", lazy=True)
