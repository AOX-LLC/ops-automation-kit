"""The audit log refuses a record that holds a gateway token, whatever field it is in."""

from __future__ import annotations

import pytest
from aox_agent_core.errors import AuditPayloadRejectedError

from opskit.core.pg.audit import checked_event
from opskit.core.ports import AuditEvent

# Built at run time so no token-shaped literal sits in the repository.
AIG_TOKEN = "aig_" + "abcdefgh" + "_" + "A" * 43
GW_TOKEN = "gw_" + "B" * 30


def _event(**payload: str) -> AuditEvent:
    return AuditEvent(action="gateway.call", actor_id="service.test", payload=payload)


@pytest.mark.parametrize("token", [AIG_TOKEN, GW_TOKEN], ids=["aig", "gw"])
def test_a_payload_that_holds_a_gateway_token_is_refused(token: str) -> None:
    with pytest.raises(AuditPayloadRejectedError):
        checked_event(_event(note=f"the call used {token}"))


def test_an_ordinary_gateway_call_record_is_accepted() -> None:
    checked_event(_event(tool="crm__get_account", outcome="Ok"))
