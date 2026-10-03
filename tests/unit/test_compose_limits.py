"""Every Compose service is resource-capped, so one runaway container cannot take the host."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

COMPOSE = yaml.safe_load((Path(__file__).parents[2] / "compose.yaml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
ONE_SHOT = {name for name, svc in SERVICES.items() if svc.get("restart") == "no"}


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_service_has_a_memory_limit(name: str) -> None:
    assert SERVICES[name].get("mem_limit"), f"{name} has no mem_limit"


@pytest.mark.parametrize("name", sorted(set(SERVICES) - ONE_SHOT - {"kit-login"}))
def test_long_running_services_have_a_pids_limit(name: str) -> None:
    assert SERVICES[name].get("pids_limit"), f"{name} has no pids_limit"


@pytest.mark.parametrize("name", sorted(ONE_SHOT | {"kit-login"}))
def test_one_shot_services_have_a_pids_limit(name: str) -> None:
    assert SERVICES[name].get("pids_limit"), f"{name} has no pids_limit"
