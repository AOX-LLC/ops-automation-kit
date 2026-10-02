"""The outbox must never hold a database transaction open while it calls n8n."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from opskit.core.stub import resume_outbox

ROW = SimpleNamespace(
    id=1,
    approval_id=uuid4(),
    run_id=uuid4(),
    payload={"decision": "approved"},
    attempts=0,
    resume_url="http://n8n:5678/webhook-waiting/1?signature=ab",
)


class FakeSession:
    def __init__(self, factory: FakeFactory) -> None:
        self.factory = factory

    async def execute(self, statement: Any) -> Any:
        self.factory.statements.append(str(statement))
        rows = [ROW] if statement.is_select and not self.factory.claimed else []
        if statement.is_select:
            self.factory.claimed = True
        return SimpleNamespace(all=lambda: rows)


class FakeFactory:
    """Stands in for async_sessionmaker; counts transactions that are currently open."""

    def __init__(self) -> None:
        self.open_transactions = 0
        self.statements: list[str] = []
        self.claimed = False

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[FakeSession]:
        self.open_transactions += 1
        try:
            yield FakeSession(self)
        finally:
            self.open_transactions -= 1


@pytest.fixture(autouse=True)
def no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def append_in(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(resume_outbox, "append_in", append_in)


@pytest.mark.parametrize("status", [200, 503])
async def test_send_happens_with_no_transaction_open(status: int) -> None:
    factory = FakeFactory()
    seen: list[int] = []

    async def send(url: str, payload: dict[str, Any]) -> int:
        seen.append(factory.open_transactions)
        return status

    delivered = await resume_outbox.deliver_due(factory, send)  # type: ignore[arg-type]
    assert seen == [0], "a transaction was open while calling n8n"
    assert delivered == (1 if status == 200 else 0)


async def test_claim_pushes_the_row_out_by_a_lease_before_sending() -> None:
    factory = FakeFactory()
    statements_at_send: list[str] = []

    async def send(url: str, payload: dict[str, Any]) -> int:
        statements_at_send.extend(factory.statements)
        return 200

    # Assert after the call: the sender's own exceptions are caught as failed deliveries.
    assert await resume_outbox.deliver_due(factory, send) == 1  # type: ignore[arg-type]
    assert statements_at_send[0].startswith("SELECT")
    assert any(s.startswith("UPDATE core.outbox") for s in statements_at_send), "no lease"


async def test_transport_errors_are_recorded_not_raised() -> None:
    factory = FakeFactory()

    async def send(url: str, payload: dict[str, Any]) -> int:
        raise ConnectionError("n8n is down")

    assert await resume_outbox.deliver_due(factory, send) == 0  # type: ignore[arg-type]
    assert factory.open_transactions == 0
