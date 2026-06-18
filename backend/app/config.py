"""Environment-driven settings. No secret ever lives in the repo."""
import os
from dataclasses import dataclass


@dataclass
class Settings:
    database_url: str = os.environ.get("DATABASE_URL", "")
    host_timezone: str = os.environ.get("HOST_TIMEZONE", "Europe/Berlin")

    gmail_user: str = os.environ.get("GMAIL_USER", "admin@intfiba.com")
    gmail_app_password: str = os.environ.get("GMAIL_APP_PASSWORD", "")
    mail_from_name: str = os.environ.get("MAIL_FROM_NAME", "IFB Bank")

    # Shared secrets that gate the internal endpoints.
    tick_token: str = os.environ.get("TICK_TOKEN", "")
    admin_token: str = os.environ.get("ADMIN_TOKEN", "")

    # Public URLs used to build links inside emails.
    public_api_url: str = os.environ.get("PUBLIC_API_URL", "https://ifb-scheduler.fly.dev")
    public_site_url: str = os.environ.get("PUBLIC_SITE_URL", "https://calender.ifcifb.com")

    cors_origins: str = os.environ.get("CORS_ORIGINS", "https://calender.ifcifb.com")


settings = Settings()
