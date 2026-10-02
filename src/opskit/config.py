"""Runtime settings. Secrets are read from files on the kit-secrets volume, never from env."""

from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

from opskit.core.ports import Mode, Tier

API_KEY_VARIABLE = "AGENT_CORE_ANTHROPIC_API_KEY"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPSKIT_", extra="ignore", frozen=True)

    mock_mode: bool = Field(default=True, validation_alias="MOCK_MODE")
    record_fixtures: bool = Field(default=False, validation_alias="RECORD_FIXTURES")
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
    fixtures_dir: Path = Path("/app/fixtures/model")
    model_tiers_file: Path = Path("/app/config/model_tiers.json")

    approver_session_hours: int = 8

    @model_validator(mode="after")
    def _live_mode_needs_a_key(self) -> Self:
        if self.record_fixtures and self.mock_mode:
            raise ValueError("RECORD_FIXTURES=true needs MOCK_MODE=false")
        if not self.mock_mode and not self.anthropic_api_key:
            raise ValueError(f"MOCK_MODE=false needs {API_KEY_VARIABLE} set to your own key")
        return self

    @property
    def mode(self) -> Mode:
        if self.mock_mode:
            return Mode.MOCK
        return Mode.RECORD if self.record_fixtures else Mode.LIVE

    def read_secret(self, name: str) -> str:
        return (self.secrets_dir / name).read_text(encoding="utf-8").strip()

    @cached_property
    def model_tiers(self) -> dict[Tier, str]:
        raw: dict[str, str] = json.loads(self.model_tiers_file.read_text(encoding="utf-8"))
        return {Tier(name): model_id for name, model_id in raw.items()}

    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.read_secret(self.db_password_secret),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
