import ast
import json
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("token", [FAKE_AIG_TOKEN, FAKE_GW_TOKEN])
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
