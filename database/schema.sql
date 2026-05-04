CREATE DATABASE IF NOT EXISTS restobar
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
USE restobar;
SET NAMES utf8mb4;

CREATE TABLE roles (
    codigo VARCHAR(30) PRIMARY KEY,
    nombre VARCHAR(80) NOT NULL,
    descripcion VARCHAR(255) NULL,
    permisos TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE preferencias_sistema (
    clave VARCHAR(80) PRIMARY KEY,
    valor TEXT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE usuarios (
    id INT AUTO_INCREMENT PRIMARY KEY,
    nickname VARCHAR(50) UNIQUE NOT NULL,
    nombre VARCHAR(100) NOT NULL,
    apellido VARCHAR(100) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    rol VARCHAR(30) NOT NULL,
    activo BOOLEAN DEFAULT TRUE,
    must_change_password BOOLEAN DEFAULT FALSE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE audit_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    usuario_id INT NULL,
    accion VARCHAR(80) NOT NULL,
    entidad VARCHAR(80) NOT NULL,
    entidad_id VARCHAR(80) NULL,
    resumen VARCHAR(255) NULL,
    detalles TEXT NULL,
    ip_address VARCHAR(45) NULL,
    user_agent VARCHAR(255) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

CREATE TABLE zonas (
    id INT AUTO_INCREMENT PRIMARY KEY,
    nombre VARCHAR(50) NOT NULL
);

CREATE TABLE mesas (
    id INT AUTO_INCREMENT PRIMARY KEY,
    numero INT NOT NULL,
    nombre_alias VARCHAR(50),
    zona_id INT NOT NULL,
    estado ENUM('disponible', 'ocupada') DEFAULT 'disponible',
    limpieza_estado ENUM('limpia', 'sucia') DEFAULT 'limpia',
    FOREIGN KEY (zona_id) REFERENCES zonas(id)
);

CREATE TABLE lista_espera (
    id INT AUTO_INCREMENT PRIMARY KEY,
    nombre_cliente VARCHAR(100) NOT NULL,
    personas INT NOT NULL DEFAULT 1,
    telefono VARCHAR(40) NULL,
    notas VARCHAR(200) NULL,
    estado ENUM('esperando', 'sentado', 'cancelado') DEFAULT 'esperando' NOT NULL,
    mesa_id INT NULL,
    usuario_id INT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP NULL,
    FOREIGN KEY (mesa_id) REFERENCES mesas(id),
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

CREATE TABLE categorias (
    id INT AUTO_INCREMENT PRIMARY KEY,
    nombre VARCHAR(50) NOT NULL,
    envia_a_cocina BOOLEAN DEFAULT FALSE
);

CREATE TABLE productos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    nombre VARCHAR(100) NOT NULL,
    imagen_url VARCHAR(255) NULL,
    categoria_id INT NOT NULL,
    precio_costo DECIMAL(10,2) DEFAULT 0.00,
    precio_venta DECIMAL(10,2) NOT NULL,
    unidad_compra VARCHAR(30) DEFAULT 'unidad',
    unidades_por_paquete INT DEFAULT 1,
    stock_actual INT DEFAULT 0,
    maneja_stock BOOLEAN DEFAULT TRUE,
    disponible BOOLEAN DEFAULT TRUE,
    FOREIGN KEY (categoria_id) REFERENCES categorias(id)
);

CREATE TABLE sesiones_caja (
    id INT AUTO_INCREMENT PRIMARY KEY,
    usuario_id INT NOT NULL,
    fecha_apertura TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fecha_cierre TIMESTAMP NULL,
    monto_apertura DECIMAL(10,2) NOT NULL,
    monto_cierre_real DECIMAL(10,2) NULL,
    estado ENUM('abierta', 'cerrada') DEFAULT 'abierta',
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

CREATE TABLE movimientos_caja (
    id INT AUTO_INCREMENT PRIMARY KEY,
    sesion_caja_id INT NOT NULL,
    tipo ENUM('ingreso', 'egreso') NOT NULL,
    concepto VARCHAR(200) NOT NULL,
    monto DECIMAL(10,2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (sesion_caja_id) REFERENCES sesiones_caja(id)
);

CREATE TABLE ordenes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    mesa_id INT NOT NULL,
    sesion_caja_id INT NOT NULL,
    usuario_id INT NOT NULL,
    nombre_cliente VARCHAR(100) NULL,
    total DECIMAL(10,2) DEFAULT 0,
    estado ENUM('abierta', 'pagada', 'cancelada') DEFAULT 'abierta',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (mesa_id) REFERENCES mesas(id),
    FOREIGN KEY (sesion_caja_id) REFERENCES sesiones_caja(id),
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

CREATE TABLE orden_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    orden_id INT NOT NULL,
    producto_id INT NOT NULL,
    cantidad INT NOT NULL DEFAULT 1,
    precio_unitario DECIMAL(10,2) NOT NULL,
    costo_unitario DECIMAL(10,2) DEFAULT 0,
    notas VARCHAR(200),
    estado ENUM('pendiente', 'listo', 'entregado', 'cancelado') DEFAULT 'pendiente',
    pagado BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (orden_id) REFERENCES ordenes(id),
    FOREIGN KEY (producto_id) REFERENCES productos(id)
);

CREATE TABLE pagos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    orden_id INT NOT NULL,
    metodo ENUM('efectivo', 'tarjeta') NOT NULL,
    monto DECIMAL(10,2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (orden_id) REFERENCES ordenes(id)
);

CREATE TABLE movimientos_inventario (
    id INT AUTO_INCREMENT PRIMARY KEY,
    producto_id INT NOT NULL,
    tipo ENUM('compra', 'venta', 'ajuste') NOT NULL,
    cantidad_paquetes INT NULL,
    cantidad_unidades INT NOT NULL,
    precio_unitario DECIMAL(10,2),
    notas VARCHAR(200),
    usuario_id INT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (producto_id) REFERENCES productos(id),
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

CREATE TABLE orden_divisiones (
    id INT AUTO_INCREMENT PRIMARY KEY,
    orden_id INT NOT NULL,
    numero_persona INT NOT NULL,
    etiqueta VARCHAR(50) NULL,
    total DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    pagada BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (orden_id) REFERENCES ordenes(id)
);

CREATE TABLE orden_division_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    division_id INT NOT NULL,
    orden_item_id INT NOT NULL,
    cantidad INT NOT NULL DEFAULT 1,
    subtotal DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    FOREIGN KEY (division_id) REFERENCES orden_divisiones(id),
    FOREIGN KEY (orden_item_id) REFERENCES orden_items(id)
);

CREATE INDEX idx_orden_items_orden ON orden_items(orden_id);
CREATE INDEX idx_orden_items_producto ON orden_items(producto_id);
CREATE INDEX idx_mov_inv_producto ON movimientos_inventario(producto_id);
CREATE INDEX idx_orden_divisiones_orden ON orden_divisiones(orden_id);
CREATE INDEX idx_orden_division_items_division ON orden_division_items(division_id);
CREATE INDEX idx_orden_division_items_item ON orden_division_items(orden_item_id);
CREATE INDEX idx_audit_logs_created ON audit_logs(created_at);
CREATE INDEX idx_audit_logs_usuario ON audit_logs(usuario_id);
CREATE INDEX idx_lista_espera_estado ON lista_espera(estado, created_at);

INSERT INTO zonas (nombre) VALUES ('Barra'), ('Patio');

INSERT INTO categorias (nombre, envia_a_cocina) VALUES
('Bebidas con alcohol', FALSE),
('Bebidas sin alcohol', FALSE),
('Platillos', TRUE),
('Cigarros', FALSE),
('Otros', FALSE);

INSERT INTO roles (codigo, nombre, descripcion, permisos) VALUES
('administrador', 'Administrador', 'Acceso completo a todas las areas del sistema.', 'auditoria.view,caja.cancel_order,caja.charge,caja.close,caja.movements,caja.open,caja.view,categorias.create,categorias.delete,categorias.edit,categorias.view,cocina.prepare,cocina.view,dashboard.view,inventario.create,inventario.view,mesas.create,mesas.delete,mesas.edit,mesas.view,ordenes.cancel_item,ordenes.create,ordenes.deliver,ordenes.items,ordenes.ticket,ordenes.view,preferencias.edit,preferencias.view,productos.availability,productos.create,productos.delete,productos.edit,productos.view,reportes.export,reportes.view,roles.create,roles.delete,roles.edit,roles.view,security.change_password,usuarios.create,usuarios.delete,usuarios.edit,usuarios.reset_password,usuarios.view,zonas.create,zonas.delete,zonas.edit,zonas.view');

INSERT INTO preferencias_sistema (clave, valor) VALUES
('business_name', 'Restobar'),
('business_tagline', 'Operacion clara para tu restaurante'),
('business_logo_url', ''),
('timezone', 'America/El_Salvador'),
('date_format', 'dd/mm/yyyy'),
('time_format', '12h'),
('sidebar_clock', 'date'),
('default_theme', 'light'),
('ticket_footer', 'Gracias por su compra.');

INSERT INTO usuarios (nickname, nombre, apellido, password_hash, rol) VALUES
('admin', 'Admin', 'General', 'pbkdf2:sha256:cambiar_este_hash', 'administrador');
