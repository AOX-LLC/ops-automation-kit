from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from opskit.api.routers import receipts as receipts_router
from opskit.config import Settings
from opskit.core.errors import ModelRefusalError, ReplayMissError
from opskit.core.ports import RunContext, Tier
from opskit.receipts import extraction
from opskit.receipts.extraction import (
    EXTRACT_PROMPT,
    MAX_IMAGE_EDGE,
    MAX_PDF_BYTES,
    ReceiptExtraction,
    extract_receipt,
    prepare,
)

SAMPLES = sorted((Path(__file__).parents[2] / "samples" / "receipts" / "inbox").iterdir())
CTX = RunContext(run_id=str(uuid4()))


def _pdf(pages: int) -> bytes:
    objects = b"1 0 obj\n<< /Type /Pages /Count %d >>\nendobj\n" % pages
    for number in range(pages):
        objects += b"%d 0 obj\n<< /Type /Page /Parent 1 0 R >>\nendobj\n" % (number + 2)
    return b"%PDF-1.4\n" + objects + b"%%EOF\n"


def _png_bytes(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (250, 250, 250)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_all_committed_samples_are_sendable() -> None:
    assert len(SAMPLES) == 30
    for sample in SAMPLES:
        prepared = prepare(sample)
        assert prepared.attachment is not None, sample.name
        assert prepared.review_reason is None
        assert len(prepared.sha256) == 64


def test_sha256_is_of_the_original_bytes(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "big.png"
    path.write_bytes(_png_bytes((3000, 100)))
    prepared = prepare(path)
    assert prepared.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert prepared.attachment is not None
    assert prepared.attachment.sha256 != prepared.sha256


def test_oversized_pdf(tmp_path: Path) -> None:
    path = tmp_path / "huge.pdf"
    path.write_bytes(_pdf(1) + b"0" * MAX_PDF_BYTES)
    assert prepare(path).review_reason == "oversized_pdf"


def test_pdf_page_cap(tmp_path: Path) -> None:
    two, three = tmp_path / "two.pdf", tmp_path / "three.pdf"
    two.write_bytes(_pdf(2))
    three.write_bytes(_pdf(3))
    assert prepare(two).attachment is not None
    assert prepare(three).review_reason == "too_many_pages"


def test_pdf_without_pages_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert prepare(path).review_reason == "unreadable_pdf"


def test_garbage_image_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "junk.png"
    path.write_bytes(b"this is not an image")
    assert prepare(path).review_reason == "unreadable_image"


def test_large_image_is_downscaled(tmp_path: Path) -> None:
    path = tmp_path / "huge.png"
    path.write_bytes(_png_bytes((6000, 6000)))
    prepared = prepare(path)
    assert prepared.attachment is not None
    assert prepared.attachment.data is not None
    with Image.open(io.BytesIO(prepared.attachment.data)) as shrunk:
        assert max(shrunk.size) <= MAX_IMAGE_EDGE
        assert shrunk.format == "PNG"


def test_decompression_bomb_is_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extraction, "MAX_IMAGE_PIXELS", 1_000)
    path = tmp_path / "bomb.png"
    path.write_bytes(_png_bytes((200, 200)))
    assert prepare(path).review_reason == "unreadable_image"


def test_unsupported_type(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    prepared = prepare(path)
    assert prepared.review_reason == "unsupported_type"
    assert prepared.attachment is None


def test_file_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extraction, "MAX_SOURCE_BYTES", 10)
    path = tmp_path / "a.png"
    path.write_bytes(_png_bytes((20, 20)))
    assert prepare(path).review_reason == "file_too_large"


class FakeModels:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result, self.error, self.calls = result, error, []

    async def call(self, prompt: Any, **kwargs: Any) -> Any:
        self.calls.append((prompt, kwargs))
        if self.error:
            raise self.error
        return self.result


def _result(output: ReceiptExtraction) -> SimpleNamespace:
    return SimpleNamespace(
        output=output,
        tier=Tier.SMALL,
        model="model-x",
        cost_usd=Decimal("0.001200"),
        latency_ms=812.4,
        replay_key="a" * 64,
    )


async def test_needs_review_never_calls_the_model(tmp_path: Path) -> None:
    path = tmp_path / "junk.png"
    path.write_bytes(b"nope")
    models = FakeModels()
    outcome = await extract_receipt(models, CTX, path)  # type: ignore[arg-type]
    assert outcome.status == "needs_review"
    assert outcome.reason == "unreadable_image"
    assert outcome.cost_usd == "0"
    assert models.calls == []


async def test_refusal_is_failed_without_model_text() -> None:
    models = FakeModels(error=ModelRefusalError("secret model words"))
    outcome = await extract_receipt(models, CTX, SAMPLES[0])  # type: ignore[arg-type]
    assert outcome.status == "failed"
    assert outcome.reason == "ModelRefusalError"
    assert "secret" not in outcome.model_dump_json()


async def test_success_maps_fields_and_cost() -> None:
    fields = ReceiptExtraction(vendor_name="Harbor Bean Co", total_cents=1875)
    models = FakeModels(result=_result(fields))
    outcome = await extract_receipt(
        models,  # type: ignore[arg-type]
        CTX,
        SAMPLES[0],
        public_path="receipts/inbox/r01.png",
    )
    assert outcome.status == "extracted"
    assert outcome.path == "receipts/inbox/r01.png"
    assert outcome.fields == fields
    assert outcome.cost_usd == "0.001200"
    assert outcome.latency_ms == 812
    assert (outcome.tier, outcome.model, outcome.replay_key) == ("small", "model-x", "a" * 64)
    prompt, kwargs = models.calls[0]
    assert prompt is EXTRACT_PROMPT
    assert kwargs["inputs"] == {"file_name": "r01.png"}
    assert kwargs["task"] == "extraction"
    assert kwargs["tier"] is None


async def test_explicit_tier_drops_task() -> None:
    models = FakeModels(result=_result(ReceiptExtraction()))
    await extract_receipt(models, CTX, SAMPLES[0], tier=Tier.MID)  # type: ignore[arg-type]
    assert models.calls[0][1]["task"] is None
    assert models.calls[0][1]["tier"] is Tier.MID


async def test_replay_miss_propagates() -> None:
    models = FakeModels(error=ReplayMissError("miss", key="k" * 64))
    with pytest.raises(ReplayMissError):
        await extract_receipt(models, CTX, SAMPLES[0])  # type: ignore[arg-type]


TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class StubRuns:
    async def get(self, run_id: Any) -> RunContext:
        return CTX


def _client(tmp_path: Path) -> TestClient:
    inbox = tmp_path / "samples" / "receipts" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.png").write_bytes(_png_bytes((10, 10)))
    (tmp_path / "secret.png").write_bytes(_png_bytes((10, 10)))
    app = FastAPI()
    app.state.settings = Settings(samples_dir=tmp_path / "samples", dropbox_dir=tmp_path / "drop")
    app.state.service_token = TOKEN
    app.state.core = SimpleNamespace(runs=StubRuns(), models=FakeModels())
    app.include_router(receipts_router.router)
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "receipts/inbox/missing.png",
        "../secret.png",
        "receipts/inbox/../../../secret.png",
        "/etc/passwd",
        "samples/receipts/inbox/a.png",
    ],
)
def test_extract_rejects_unlisted_paths(tmp_path: Path, path: str) -> None:
    response = _client(tmp_path).post(
        "/v1/receipts/extract", json={"run_id": str(uuid4()), "path": path}, headers=AUTH
    )
    assert response.status_code == 404


def test_extract_requires_the_service_token(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/v1/receipts/extract", json={"run_id": str(uuid4()), "path": "x"}
    )
    assert response.status_code == 401
