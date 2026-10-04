import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from opskit.config import Settings
from opskit.gateway import GatewayClient, build_gateway_client
from opskit.gateway.mcp_transport import McpTransport

# Built at runtime so this file holds no token-shaped literal.
FAKE_TOKEN = "aig_" + "zyxwvuts" + "_" + "Q" * 43


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPSKIT_GATEWAY_ENABLED",
        "OPSKIT_GATEWAY_MODE",
        "OPSKIT_GATEWAY_URL",
        "OPSKIT_GATEWAY_TIMEOUT_S",
        "GATEWAY_TOKEN",
        "GATEWAY_TOKEN_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_the_gateway_is_off_by_default() -> None:
    assert Settings().gateway_enabled is False


def test_the_default_mode_is_replay() -> None:
    assert Settings().gateway_mode == "replay"


def test_the_default_url_is_the_local_gateway() -> None:
    assert Settings().gateway_url == "http://127.0.0.1:4401/mcp"


def test_a_disabled_gateway_builds_no_client() -> None:
    assert build_gateway_client(Settings()) is None


def test_replay_builds_a_client_without_any_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSKIT_GATEWAY_ENABLED", "true")
    assert isinstance(build_gateway_client(Settings()), GatewayClient)


def test_building_a_replay_client_does_not_import_the_mcp_transport() -> None:
    script = (
        "import sys\n"
        "from opskit.config import Settings\n"
        "from opskit.gateway import build_gateway_client\n"
        "client = build_gateway_client(Settings(gateway_enabled=True))\n"
        "assert client is not None\n"
        "assert 'opskit.gateway.mcp_transport' not in sys.modules\n"
        "assert 'mcp' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", ["live", "record"])
def test_a_spending_mode_without_a_token_is_a_validation_error(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("OPSKIT_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("OPSKIT_GATEWAY_MODE", mode)
    with pytest.raises(ValidationError, match="GATEWAY_TOKEN") as raised:
        Settings()
    assert "aig_" not in str(raised.value)


def test_the_token_file_is_read_and_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(f"  {FAKE_TOKEN}\n", encoding="utf-8")
    monkeypatch.setenv("GATEWAY_TOKEN_FILE", str(token_file))
    assert Settings().read_gateway_token().get_secret_value() == FAKE_TOKEN


def test_the_token_variable_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_TOKEN", FAKE_TOKEN)
    assert Settings().read_gateway_token().get_secret_value() == FAKE_TOKEN


def test_the_token_never_appears_in_repr_or_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_TOKEN", FAKE_TOKEN)
    settings = Settings()
    assert FAKE_TOKEN not in repr(settings)
    assert FAKE_TOKEN not in str(settings)


def test_a_timeout_below_50_seconds_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSKIT_GATEWAY_TIMEOUT_S", "49")
    with pytest.raises(ValidationError):
        Settings()


def test_the_transport_refuses_a_timeout_below_the_approval_hold() -> None:
    with pytest.raises(ValueError, match="timeout"):
        McpTransport("http://127.0.0.1:4401/mcp", SecretStr(FAKE_TOKEN), 10)
