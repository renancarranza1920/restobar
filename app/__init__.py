from datetime import timezone

from flask import Flask, redirect, request, session, url_for
from flask_login import current_user

from .config import Config
from .extensions import db, login_manager, migrate
from .services import (
    bootstrap_admin_account,
    bootstrap_roles_permissions,
    bootstrap_security_schema,
    bootstrap_system_preferences,
    bootstrap_takeout_table,
    bootstrap_waitlist_schema,
    business_initial,
    format_local_datetime,
    get_active_cash_session,
    get_system_preferences,
    get_user,
    local_now,
    money,
    navigation_for_user,
    role_label,
    theme_choices,
    time_ago_label,
    user_can,
)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config["PRODUCT_UPLOAD_DIR"].mkdir(parents=True, exist_ok=True)
    app.config["BRANDING_UPLOAD_DIR"].mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    from . import models
    from .routes import api_bp, web_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp)

    with app.app_context():
        bootstrap_security_schema()
        bootstrap_system_preferences()
        bootstrap_waitlist_schema()
        bootstrap_roles_permissions()
        bootstrap_admin_account()
        bootstrap_takeout_table()

    @login_manager.user_loader
    def load_user(user_id):
        return get_user(user_id)

    @app.template_filter("money")
    def money_filter(value):
        return f"${money(value):,.2f}"

    @app.template_filter("datetime_short")
    def datetime_short(value):
        if not value:
            return "--"
        return format_local_datetime(value, "datetime")

    @app.template_filter("datetime_iso")
    def datetime_iso(value):
        if not value:
            return ""
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    @app.template_filter("time_ago")
    def time_ago_filter(value):
        return time_ago_label(value)

    @app.template_filter("date_input")
    def date_input_filter(value):
        if not value:
            return ""
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    @app.context_processor
    def inject_global_context():
        preferences = get_system_preferences()
        theme = session.get("theme", preferences["default_theme"])
        if theme not in theme_choices():
            theme = preferences["default_theme"]

        return {
            "app_name": preferences["business_name"],
            "business_logo_url": preferences["business_logo_url"],
            "business_initial": business_initial(preferences),
            "business_tagline": preferences["business_tagline"],
            "system_preferences": preferences,
            "current_theme": theme,
            "nav_items": (
                navigation_for_user(current_user)
                if current_user.is_authenticated
                else []
            ),
            "active_session": (
                get_active_cash_session()
                if current_user.is_authenticated and user_can(current_user, "caja.view")
                else None
            ),
            "role_label": role_label,
            "user_can": user_can,
            "now_label": format_local_datetime(local_now(), preferences["sidebar_clock"], preferences),
        }

    @app.before_request
    def require_password_change():
        if not current_user.is_authenticated:
            return None
        if not getattr(current_user, "must_change_password", False):
            return None
        allowed_endpoints = {
            "web.mi_seguridad",
            "web.actualizar_mi_password",
            "web.logout",
            "web.cambiar_tema",
            "static",
        }
        if request.endpoint in allowed_endpoints or (request.endpoint or "").startswith("static"):
            return None
        return redirect(url_for("web.mi_seguridad"))

    @app.shell_context_processor
    def make_shell_context():
        return {"db": db, "models": models}

    return app
