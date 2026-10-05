import ast
import json
from pathlib import Path

import pytest

from opskit.core.ports import RunContext
from opskit.gateway.client import GatewayClient, classify
from opskit.gateway.outcomes import (
    ApprovalRejected,
    NotAvailable,
    Ok,
    Pending,
    PolicyRefused,
    Unauthorized,
)
from opskit.gateway.recordings import (
    TOKEN_PATTERN,
    Recording,
    RecordingError,
    RecordingRefusedError,
    RecordingStore,
    RecordingTransport,
    ReplayTransport,
)
from opskit.gateway.transport import JsonObject, RawOutcome

ROOT = Path(__file__).resolve().parents[2]
ARGS: JsonObject = {"query": "acme", "limit": 2}
PENDING = RawOutcome(
    kind="result",
    is_error=True,
    structured={"status": "approval_pending", "approval_id": "abc"},
)
REJECTED = RawOutcome(kind="rpc_error", code=-32010, message="An approver rejected this call.")
# Built at runtime so this file holds no token-shaped literal.
FAKE_AIG_TOKEN = "aig_" + "abcdefgh" + "_" + "A" * 43
FAKE_GW_TOKEN = "gw_" + "B" * 30


class FakeInner:
    def __init__(self, *outcomes: RawOutcome) -> None:
        self.queue = list(outcomes)

    async def call(
        self, tool: str, arguments: JsonObject, *, meta: JsonObject | None = None
    ) -> RawOutcome:
        return self.queue.pop(0)


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    recording = Recording("crm__search_accounts", "", ARGS, (PENDING, REJECTED))
    store.save(recording)
    assert store.load("crm__search_accounts", ARGS, "") == recording


def test_the_file_name_starts_with_the_tool_name(tmp_path: Path) -> None:
    path = RecordingStore(tmp_path).save(Recording("crm__get_account", "", ARGS, (REJECTED,)))
    assert path.name.startswith("crm__get_account.")


def test_loading_an_unrecorded_call_gives_none(tmp_path: Path) -> None:
    assert RecordingStore(tmp_path).load("crm__get_account", ARGS, "") is None


async def test_two_outcomes_for_one_call_replay_in_order_and_a_third_is_refused(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    recorder = RecordingTransport(FakeInner(PENDING, REJECTED), store)
    await recorder.call("tickets__create_ticket", ARGS)
    await recorder.call("tickets__create_ticket", ARGS)
    replay = ReplayTransport(store)
    assert await replay.call("tickets__create_ticket", ARGS) == PENDING
    assert await replay.call("tickets__create_ticket", ARGS) == REJECTED
    with pytest.raises(RecordingError):
        await replay.call("tickets__create_ticket", ARGS)


async def test_the_same_call_in_two_scenarios_is_two_recordings(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    unauthorized = RawOutcome(kind="http_status", status=401)
    fine = RawOutcome(kind="result", text="fine")
    await RecordingTransport(FakeInner(unauthorized), store, "unauthorized").call("crm__x", ARGS)
    await RecordingTransport(FakeInner(fine), store, "").call("crm__x", ARGS)
    assert await ReplayTransport(store, "unauthorized").call("crm__x", ARGS) == unauthorized
    assert await ReplayTransport(store, "").call("crm__x", ARGS) == fine
    assert _recording_files(tmp_path, "crm__x") == 2


async def test_replay_of_an_unrecorded_call_names_the_tool(tmp_path: Path) -> None:
    with pytest.raises(RecordingError, match="crm__get_account"):
        await ReplayTransport(RecordingStore(tmp_path)).call("crm__get_account", ARGS)


async def test_a_new_recording_session_replaces_the_old_outcomes(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    first = RecordingTransport(FakeInner(PENDING, REJECTED), store)
    await first.call("crm__x", ARGS)
    await first.call("crm__x", ARGS)
    await RecordingTransport(FakeInner(REJECTED), store).call("crm__x", ARGS)
    recording = store.load("crm__x", ARGS, "")
    assert recording is not None
    assert recording.outcomes == (REJECTED,)


@pytest.mark.parametrize("token", [FAKE_AIG_TOKEN, FAKE_GW_TOKEN], ids=["aig", "gw"])
def test_saving_an_outcome_that_holds_a_token_is_refused(tmp_path: Path, token: str) -> None:
    leaky = RawOutcome(kind="result", text=f"your key is {token}")
    store = RecordingStore(tmp_path)
    with pytest.raises(RecordingRefusedError):
        store.save(Recording("crm__x", "", ARGS, (leaky,)))
    assert list(tmp_path.iterdir()) == []


def test_the_token_pattern_matches_the_runtime_built_tokens() -> None:
    assert TOKEN_PATTERN.search(FAKE_AIG_TOKEN)
    assert TOKEN_PATTERN.search(FAKE_GW_TOKEN)


def _recording_files(directory: Path, tool: str) -> int:
    return len(list(directory.glob(f"{tool}.*.json")))


def _committed_recordings() -> list[Path]:
    return sorted((ROOT / "fixtures" / "gateway").glob("*.json"))


def test_every_committed_recording_loads_and_holds_no_token() -> None:
    files = _committed_recordings()
    if not files:
        pytest.skip("no committed gateway recordings")
    store = RecordingStore(ROOT / "fixtures" / "gateway")
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert TOKEN_PATTERN.search(text) is None, path.name
        document = json.loads(text)
        loaded = store.load(document["tool"], document["arguments"], document["scenario"])
        assert loaded is not None, path.name


def _imports_mcp_or_httpx2(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        if any(name.split(".")[0] in {"mcp", "httpx2"} for name in names):
            return True
    return False


def test_only_the_mcp_transport_imports_the_sdk_or_its_http_client() -> None:
    source = ROOT / "src" / "opskit"
    importers = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*.py")
        if _imports_mcp_or_httpx2(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert importers == {"gateway/mcp_transport.py"}


COMMITTED_RECORDINGS = Path(__file__).resolve().parents[2] / "fixtures" / "gateway"


def _replayed_client(scenario: str = "") -> GatewayClient:
    store = RecordingStore(COMMITTED_RECORDINGS)
    return GatewayClient(ReplayTransport(store, scenario))


async def test_the_recorded_reads_replay_as_ok_through_the_client() -> None:
    client = _replayed_client()
    ctx = RunContext(run_id="run-1")

    assert isinstance(await client.search_accounts(ctx, "Pier Nine", limit=5), Ok)
    assert isinstance(await client.get_account(ctx, "ACC-00003"), Ok)
    assert isinstance(await client.list_deals(ctx, "ACC-00003", limit=5), Ok)


async def test_the_recorded_ticket_replays_as_pending_then_rejected() -> None:
    client = _replayed_client()
    ctx = RunContext(run_id="run-1")
    story = {
        "account_id": "ACC-00003",
        "subject": "Damaged pallets on order HS-48213",
        "description": (
            "Customer Marta (Pier Nine Supply, ACC-00003) reports that two of four pallets on "
            "order HS-48213 arrived damaged: torn shrink wrap and crushed cartons. She asks for "
            "replacements and return instructions."
        ),
    }

    first = await client.create_ticket(ctx, **story)
    second = await client.create_ticket(ctx, **story)

    assert isinstance(first, Pending)
    assert first.approval_id
    assert isinstance(second, ApprovalRejected)


async def test_the_recorded_bad_token_replays_as_unauthorized() -> None:
    store = RecordingStore(COMMITTED_RECORDINGS)
    raw = await ReplayTransport(store, "unauthorized").call(
        "crm__get_account", {"account_id": "ACC-00003"}
    )

    assert isinstance(classify(raw), Unauthorized)


async def test_the_recorded_refusals_replay_with_their_request_id_and_codes() -> None:
    store = RecordingStore(COMMITTED_RECORDINGS)
    no_limit = await ReplayTransport(store, "no-limit").call(
        "crm__search_accounts", {"query": "Pier Nine"}
    )
    out_of_scope = await ReplayTransport(store, "out-of-scope").call(
        "tickets__add_comment", {"ticket_id": "TKT-000001", "body": "x"}
    )

    refused = classify(no_limit)
    assert isinstance(refused, PolicyRefused)
    assert refused.request_id
    assert isinstance(classify(out_of_scope), NotAvailable)


def test_a_recording_with_an_unknown_kind_or_field_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="kind"):
        RawOutcome.from_json({"kind": "bogus"})
    with pytest.raises(ValueError, match="unknown outcome fields"):
        RawOutcome.from_json({"kind": "result", "surprise": 1})


def test_a_recording_that_holds_another_call_is_not_served(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    path = store.save(
        Recording("crm__get_account", "", {"account_id": "A"}, (RawOutcome(kind="result"),))
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    document["arguments"] = {"account_id": "B"}
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RecordingError, match="another call"):
        store.load("crm__get_account", {"account_id": "A"}, "")


def test_a_tool_name_cannot_leave_the_recordings_folder(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)

    with pytest.raises(RecordingError, match="tool name"):
        store.load("../escape", {}, "")
