"""Runtime settings. Secrets are read from files on the kit-secrets volume, never from env."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

from opskit.core.ports import Mode

API_KEY_VARIABLE = "AGENT_CORE_ANTHROPIC_API_KEY"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPSKIT_", extra="ignore", frozen=True)

    # agent-core's mode: replay (default, never spends) | live | record.
    agent_core_mode: Mode = Field(default=Mode.REPLAY, validation_alias="AGENT_CORE_MODE")
    agent_core_config: Path = Field(
        default=Path("/app/config/agent-core.toml"), validation_alias="AGENT_CORE_CONFIG"
    )
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias=API_KEY_VARIABLE)

    # Whether the 15 and 5 minute schedules in the receipts and inbox workflows start runs. Off,
    # only a webhook call does: the smoke test and the recorded demo need runs that happen when
    # they say, not when a clock boundary falls.
    scheduled_runs: bool = True

    # Where company research reads from. "web" fetches each company's own public site and is
    # for the manual live demo only: it needs AGENT_CORE_MODE=live.
    leads_retrieval: Literal["corpus", "web"] = Field(
        default="corpus", validation_alias="LEADS_RETRIEVAL"
    )

    secrets_dir: Path = Path("/run/kit-secrets")
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "opskit"
    db_user: str = "opskit_app"
    db_password_secret: str = "opskit_app_password"  # noqa: S105 - a file name, not a value
    # The approver role: used by the approver page's decision path and nothing else.
    approver_db_user: str = "opskit_approver"
    approver_db_password_secret: str = "opskit_approver_password"  # noqa: S105 - a file name

    n8n_public_url: str = "http://localhost:4300"
    n8n_internal_url: str = "http://n8n:5678"
    mailpit_api_url: str = "http://mailpit:8025"
    mailpit_smtp_host: str = "mailpit"
    mailpit_smtp_port: int = 1025

    samples_dir: Path = Path("/data/samples")
    dropbox_dir: Path = Path("/data/dropbox")

    # The sessions table refuses an expiry more than 30 days out; one hour short of it, so a
    # daylight-saving change in the server's time zone cannot push the maximum over.
    approver_session_hours: int = Field(default=8, ge=1, le=719)
    # Shown beside "Approvals"; empty disables. Must be same-origin (the CSP blocks others).
    brand_logo_url: str | None = "/static/brand/aox-logo-black.png"
    # The logo's alt text. Change it together with the logo, so a screen reader names yours.
    brand_logo_alt: str = "AOX"
    # Shown instead of the logo in the dark theme. Left unset, it is derived: a logo whose file
    # name ends in "-black.png" gets its "-white.png" twin; any other logo serves both themes.
    brand_logo_dark_url: str | None = None

    @field_validator("brand_logo_url", "brand_logo_dark_url")
    @classmethod
    def _logo_is_same_origin(cls, value: str | None) -> str | None:
        if value and (not value.startswith("/") or value.startswith("//")):
            raise ValueError("OPSKIT_BRAND_LOGO_URL must be a path on this site, like /static/...")
        return value or None

    @property
    def brand_logo_dark_url_resolved(self) -> str | None:
        if self.brand_logo_dark_url:
            return self.brand_logo_dark_url
        if self.brand_logo_url and self.brand_logo_url.endswith("-black.png"):
            return self.brand_logo_url.removesuffix("-black.png") + "-white.png"
        return None

    @model_validator(mode="after")
    def _live_mode_needs_a_key(self) -> Self:
        if self.agent_core_mode is not Mode.REPLAY and not self.anthropic_api_key:
            raise ValueError(
                f"AGENT_CORE_MODE={self.agent_core_mode.value} needs {API_KEY_VARIABLE} "
                "set to your own key"
            )
        return self

    @model_validator(mode="after")
    def _web_retrieval_needs_live_mode(self) -> Self:
        if self.leads_retrieval == "web" and self.agent_core_mode is not Mode.LIVE:
            raise ValueError("LEADS_RETRIEVAL=web needs AGENT_CORE_MODE=live")
        return self

    @property
    def mode(self) -> Mode:
        return self.agent_core_mode

    def read_secret(self, name: str) -> str:
        return (self.secrets_dir / name).read_text(encoding="utf-8").strip()

    def database_url(self, *, approver: bool = False) -> URL:
        """The requester role's URL, or with `approver=True` the approver role's."""
        user = self.approver_db_user if approver else self.db_user
        secret = self.approver_db_password_secret if approver else self.db_password_secret
        return URL.create(
            "postgresql+psycopg",
            username=user,
            password=self.read_secret(secret),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
