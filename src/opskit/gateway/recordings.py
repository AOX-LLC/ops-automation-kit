"""Recordings of real gateway answers, and the two transports that use them.

One file per call, named by the call's key. A file holds the outcomes in the order the gateway
gave them, so a write that came back pending and then rejected replays as pending and then
rejected. Replay is strict: a call with no recording, or one asked more often than it was
recorded, is an error and never a guess. Recordings are made only by RecordingTransport, from a
real run; nothing here writes one by hand.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opskit.core.secrets import GATEWAY_TOKEN_PATTERNS
from opskit.gateway.transport import (
    PROTOCOL_VERSION,
    JsonObject,
    RawOutcome,
    ToolTransport,
    call_key,
)

FORMAT = 1
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# The gateway's bearer tokens (aig_<lookup id>_<secret>) and the shape its integration notes name.
TOKEN_PATTERN = re.compile("|".join(GATEWAY_TOKEN_PATTERNS.values()))


class RecordingError(Exception):
    """Replay was asked for something that was not recorded."""


class RecordingRefusedError(Exception):
    """A recording was refused because it held something that looks like a token."""


def contains_token(text: str) -> bool:
    return TOKEN_PATTERN.search(text) is not None


@dataclass(frozen=True, slots=True)
class Recording:
    tool: str
    scenario: str
    arguments: JsonObject
    outcomes: tuple[RawOutcome, ...]


class RecordingStore:
    def __init__(self, directory: Path, recorded_with: dict[str, str] | None = None) -> None:
        self._directory = directory
        self._recorded_with = recorded_with or {"protocol": PROTOCOL_VERSION}

    def note_protocol(self, version: str) -> None:
        """The protocol version the live session actually negotiated, for the recordings' header."""
        self._recorded_with = {**self._recorded_with, "protocol": version}

    def _path(self, tool: str, key: str) -> Path:
        if not TOOL_NAME.fullmatch(tool):
            raise RecordingError(f"{tool!r} is not a tool name a recording can be named after.")
        return self._directory / f"{tool}.{key[:16]}.json"

    def load(self, tool: str, arguments: JsonObject, scenario: str) -> Recording | None:
        path = self._path(tool, call_key(tool, arguments, scenario))
        if not path.is_file():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
        try:
            if document["format"] != FORMAT:
                raise ValueError(f"format {document['format']!r}")
            if (document["tool"], document["scenario"], document["arguments"]) != (
                tool,
                scenario,
                arguments,
            ):
                raise ValueError("the file holds another call")
            outcomes = tuple(RawOutcome.from_json(o) for o in document["outcomes"])
        except (KeyError, TypeError, ValueError) as error:
            raise RecordingError(f"The recording {path.name} is not usable: {error}") from error
        return Recording(tool, scenario, arguments, outcomes)

    def save(self, recording: Recording) -> Path:
        document: dict[str, Any] = {
            "format": FORMAT,
            "recorded_with": self._recorded_with,
            "tool": recording.tool,
            "scenario": recording.scenario,
            "arguments": recording.arguments,
            "outcomes": [outcome.to_json() for outcome in recording.outcomes],
        }
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if contains_token(text):
            raise RecordingRefusedError(
                f"The recording of {recording.tool} holds something that looks like a token."
            )
        key = call_key(recording.tool, recording.arguments, recording.scenario)
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path(recording.tool, key)
        path.write_text(text, encoding="utf-8")
        return path


class ReplayTransport:
    """Hands back each call's recorded outcomes in order, one per call."""

    def __init__(self, store: RecordingStore, scenario: str = "") -> None:
        self._store = store
        self._scenario = scenario
        self._served: dict[str, int] = {}

    async def call(
        self, tool: str, arguments: JsonObject, *, meta: JsonObject | None = None
    ) -> RawOutcome:
        recording = self._store.load(tool, arguments, self._scenario)
        if recording is None:
            raise RecordingError(
                f"No gateway recording for {tool} in scenario {self._scenario!r} with these "
                "arguments. Recordings come only from a real run (scripts/gateway_live_run.py)."
            )
        key = call_key(tool, arguments, self._scenario)
        position = self._served.get(key, 0)
        if position >= len(recording.outcomes):
            raise RecordingError(
                f"{tool} was recorded {len(recording.outcomes)} time(s) and has been asked "
                f"{position + 1} times in scenario {self._scenario!r}."
            )
        self._served[key] = position + 1
        return recording.outcomes[position]


@dataclass
class RecordingTransport:
    """Forwards to a real transport and writes down what came back. The first answer for a call
    in a session replaces the file's old outcomes; later ones are appended."""

    inner: ToolTransport
    store: RecordingStore
    scenario: str = ""
    _session: dict[str, list[RawOutcome]] = field(default_factory=dict)

    async def call(
        self, tool: str, arguments: JsonObject, *, meta: JsonObject | None = None
    ) -> RawOutcome:
        outcome = await self.inner.call(tool, arguments, meta=meta)
        negotiated = getattr(self.inner, "protocol_version", None)
        if negotiated:
            self.store.note_protocol(negotiated)
        key = call_key(tool, arguments, self.scenario)
        seen = self._session.setdefault(key, [])
        seen.append(outcome)
        self.store.save(Recording(tool, self.scenario, arguments, tuple(seen)))
        return outcome
