import pytest
from pydantic import ValidationError

from opskit.config import API_KEY_VARIABLE, Settings
from opskit.core.ports import Mode


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENT_CORE_MODE", API_KEY_VARIABLE, "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_replay_is_the_default() -> None:
    assert Settings().mode is Mode.REPLAY


@pytest.mark.parametrize("mode", ["live", "record"])
def test_spending_modes_without_key_name_the_variable(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("AGENT_CORE_MODE", mode)
    with pytest.raises(ValidationError, match=API_KEY_VARIABLE):
        Settings()


def test_the_generic_anthropic_variable_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CORE_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    with pytest.raises(ValidationError, match=API_KEY_VARIABLE):
        Settings()


def test_key_present_but_mode_unset_stays_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "sk-test-placeholder")
    assert Settings().mode is Mode.REPLAY


def test_live_and_record_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "sk-test-placeholder")
    monkeypatch.setenv("AGENT_CORE_MODE", "live")
    assert Settings().mode is Mode.LIVE
    monkeypatch.setenv("AGENT_CORE_MODE", "record")
    assert Settings().mode is Mode.RECORD


def test_key_never_appears_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "sk-test-placeholder")
    assert "sk-test-placeholder" not in repr(Settings())
