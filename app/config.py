from datetime import timedelta
from os import getenv
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv


load_dotenv()


def env_flag(name, default="0"):
    return getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    BASE_DIR = Path(__file__).resolve().parent.parent
    SECRET_KEY = getenv("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JSON_SORT_KEYS = False
    MAX_CONTENT_LENGTH = 4 * 1024 * 1024

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = env_flag("COOKIE_SECURE", "0")
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    REMEMBER_COOKIE_DURATION = timedelta(days=14)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)

    DEFAULT_ADMIN_NICKNAME = getenv("DEFAULT_ADMIN_NICKNAME", "admin")
    DEFAULT_ADMIN_PASSWORD = getenv("DEFAULT_ADMIN_PASSWORD", "admin123")
    APP_TIMEZONE = getenv("APP_TIMEZONE", "America/El_Salvador")
    TAKEOUT_TABLE_ID = int(getenv("TAKEOUT_TABLE_ID", "999"))
    TAKEOUT_TABLE_NUMBER = int(getenv("TAKEOUT_TABLE_NUMBER", "999"))
    TAKEOUT_TABLE_ALIAS = getenv("TAKEOUT_TABLE_ALIAS", "Para llevar")
    TAKEOUT_ZONE_NAME = getenv("TAKEOUT_ZONE_NAME", "Sistema")
    PRODUCT_UPLOAD_DIR = BASE_DIR / "app" / "static" / "uploads" / "products"
    BRANDING_UPLOAD_DIR = BASE_DIR / "app" / "static" / "uploads" / "branding"

    _database_url = getenv("DATABASE_URL")

    if _database_url:
        SQLALCHEMY_DATABASE_URI = _database_url
    else:
        db_user = getenv("DB_USER", "root")
        db_password = quote_plus(getenv("DB_PASSWORD", ""))
        db_host = getenv("DB_HOST", "127.0.0.1")
        db_port = getenv("DB_PORT", "3306")
        db_name = getenv("DB_NAME", "restobar")

        SQLALCHEMY_DATABASE_URI = (
            f"mysql+pymysql://{db_user}:{db_password}@"
            f"{db_host}:{db_port}/{db_name}?charset=utf8mb4"
        )
