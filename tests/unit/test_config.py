import pytest
from pydantic import ValidationError

from opskit.config import API_KEY_VARIABLE, Settings
from opskit.core.ports import Mode


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MOCK_MODE", "RECORD_FIXTURES", API_KEY_VARIABLE, "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_mock_mode_is_the_default() -> None:
    assert Settings().mode is Mode.MOCK


def test_live_mode_without_key_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_MODE", "false")
    with pytest.raises(ValidationError, match=API_KEY_VARIABLE):
        Settings()


def test_the_generic_anthropic_variable_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    with pytest.raises(ValidationError, match=API_KEY_VARIABLE):
        Settings()


def test_key_present_but_mock_unset_stays_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "sk-test-placeholder")
    assert Settings().mode is Mode.MOCK


def test_live_and_record_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv(API_KEY_VARIABLE, "sk-test-placeholder")
    assert Settings().mode is Mode.LIVE
    monkeypatch.setenv("RECORD_FIXTURES", "true")
    assert Settings().mode is Mode.RECORD


def test_recording_needs_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECORD_FIXTURES", "true")
    with pytest.raises(ValidationError, match="MOCK_MODE=false"):
        Settings()


def test_key_never_appears_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "sk-test-placeholder")
    assert "sk-test-placeholder" not in repr(Settings())
