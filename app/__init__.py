from datetime import timezone

from flask import Flask, session
from flask_login import current_user

from .config import Config
from .extensions import db, login_manager, migrate
from .services import (
    bootstrap_admin_account,
    bootstrap_takeout_table,
    default_theme,
    get_active_cash_session,
    get_user,
    local_now,
    localize_datetime,
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

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    from . import models
    from .routes import api_bp, web_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp)

    with app.app_context():
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
        localized_value = localize_datetime(value)
        return localized_value.strftime("%d/%m/%Y %I:%M %p")

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
        theme = session.get("theme", default_theme())
        if theme not in theme_choices():
            theme = default_theme()

        return {
            "app_name": "Restobar",
            "current_theme": theme,
            "nav_items": (
                navigation_for_user(current_user)
                if current_user.is_authenticated
                else []
            ),
            "active_session": (
                get_active_cash_session()
                if current_user.is_authenticated and user_can(current_user, "caja")
                else None
            ),
            "role_label": role_label,
            "user_can": user_can,
            "now_label": local_now().strftime("%d/%m/%Y"),
        }

    @app.shell_context_processor
    def make_shell_context():
        return {"db": db, "models": models}

    return app
