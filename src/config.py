"""Application settings, read once from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(v.strip().lower() for v in value.split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    zaptec_api_url: str = "https://api.zaptec.com"
    database_path: str = str(BASE_DIR / "data" / "zaptecfaktura.db")
    # Optional: Zaptec usernames (emails) allowed to log in. Empty = everyone
    # with a Zaptec account. Access to Fiken is controlled separately by the
    # Fiken app's approved users, since each user connects their own Fiken.
    allowed_users: frozenset[str] = field(default_factory=frozenset)
    # Encrypts stored Fiken tokens. Required when Fiken is configured.
    secret_key: str = ""
    session_max_age_seconds: int = 12 * 60 * 60
    cookie_secure: bool = True

    fiken_api_url: str = "https://api.fiken.no/api/v2"
    fiken_oauth_url: str = "https://fiken.no/oauth"
    fiken_client_id: str = ""
    fiken_client_secret: str = ""
    # Must match a redirect URI registered on the Fiken app, e.g.
    # https://faktura.example.no/fiken/callback
    fiken_redirect_uri: str = ""
    fiken_days_until_due: int = 14
    # Either a Fiken product (carries VAT type and income account), or a free
    # text line with the VAT type and income account below.
    fiken_product_id: int | None = None
    fiken_vat_type: str = "HIGH"
    fiken_income_account: str = "3000"

    @property
    def fiken_enabled(self) -> bool:
        return bool(
            self.fiken_client_id
            and self.fiken_client_secret
            and self.fiken_redirect_uri
            and self.secret_key
        )


def load_settings() -> Settings:
    env = os.environ
    product = env.get("FIKEN_PRODUCT_ID", "").strip()
    return Settings(
        zaptec_api_url=env.get("ZAPTEC_API_URL", Settings.zaptec_api_url),
        database_path=env.get("DATABASE_PATH", Settings.database_path),
        allowed_users=_csv(env.get("ALLOWED_USERS")),
        secret_key=env.get("SECRET_KEY", ""),
        session_max_age_seconds=int(
            env.get("SESSION_MAX_AGE_SECONDS", Settings.session_max_age_seconds)
        ),
        cookie_secure=_bool(env.get("COOKIE_SECURE"), True),
        fiken_api_url=env.get("FIKEN_API_URL", Settings.fiken_api_url),
        fiken_oauth_url=env.get("FIKEN_OAUTH_URL", Settings.fiken_oauth_url),
        fiken_client_id=env.get("FIKEN_CLIENT_ID", ""),
        fiken_client_secret=env.get("FIKEN_CLIENT_SECRET", ""),
        fiken_redirect_uri=env.get("FIKEN_REDIRECT_URI", ""),
        fiken_days_until_due=int(
            env.get("FIKEN_DAYS_UNTIL_DUE", Settings.fiken_days_until_due)
        ),
        fiken_product_id=int(product) if product else None,
        fiken_vat_type=env.get("FIKEN_VAT_TYPE", Settings.fiken_vat_type),
        fiken_income_account=env.get(
            "FIKEN_INCOME_ACCOUNT", Settings.fiken_income_account
        ),
    )


settings = load_settings()
