"""Runtime settings. Secrets are read from files on the kit-secrets volume, never from env."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
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

    secrets_dir: Path = Path("/run/kit-secrets")
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "opskit"
    db_user: str = "opskit_app"
    db_password_secret: str = "opskit_app_password"  # noqa: S105 - a file name, not a value

    n8n_public_url: str = "http://localhost:4300"
    n8n_internal_url: str = "http://n8n:5678"
    mailpit_api_url: str = "http://mailpit:8025"
    mailpit_smtp_host: str = "mailpit"
    mailpit_smtp_port: int = 1025

    samples_dir: Path = Path("/data/samples")
    dropbox_dir: Path = Path("/data/dropbox")

    approver_session_hours: int = 8
    # Shown beside "Approvals"; empty disables. Must be same-origin (the CSP blocks others).
    brand_logo_url: str | None = "/static/brand/logo.svg"

    @model_validator(mode="after")
    def _live_mode_needs_a_key(self) -> Self:
        if self.agent_core_mode is not Mode.REPLAY and not self.anthropic_api_key:
            raise ValueError(
                f"AGENT_CORE_MODE={self.agent_core_mode.value} needs {API_KEY_VARIABLE} "
                "set to your own key"
            )
        return self

    @property
    def mode(self) -> Mode:
        return self.agent_core_mode

    def read_secret(self, name: str) -> str:
        return (self.secrets_dir / name).read_text(encoding="utf-8").strip()

    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.read_secret(self.db_password_secret),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
