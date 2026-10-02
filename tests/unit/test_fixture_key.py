from opskit.core.fixtures import fixture_key, fixture_path, request_digest
from opskit.core.ports import Attachment, PromptRef, Tier

PROMPT = PromptRef(id="receipts.extract", version=1, template="Extract the fields.")


def key_for(inputs: dict[str, object], attachments: tuple[Attachment, ...] = ()) -> str:
    digest = request_digest(
        prompt=PROMPT,
        tier=Tier.SMALL,
        schema_name="Receipt",
        inputs=inputs,
        attachments=attachments,
    )
    return fixture_key(digest)


def test_input_key_order_does_not_matter() -> None:
    assert key_for({"a": 1, "b": [1, 2]}) == key_for({"b": [1, 2], "a": 1})


def test_different_inputs_give_different_keys() -> None:
    assert key_for({"a": 1}) != key_for({"a": 2})


def test_one_attachment_byte_changes_the_key() -> None:
    first = Attachment(media_type="image/png", data=b"\x89PNG-receipt-1")
    second = Attachment(media_type="image/png", data=b"\x89PNG-receipt-2")
    assert key_for({}, (first,)) != key_for({}, (second,))


def test_attachment_bytes_never_enter_the_digest() -> None:
    digest = request_digest(
        prompt=PROMPT,
        tier=Tier.SMALL,
        schema_name="Receipt",
        inputs={},
        attachments=(Attachment(media_type="image/png", data=b"secret-bytes"),),
    )
    assert "secret-bytes" not in str(digest)


def test_prompt_version_and_tier_are_part_of_the_key() -> None:
    base = request_digest(
        prompt=PROMPT, tier=Tier.SMALL, schema_name="R", inputs={}, attachments=()
    )
    bumped = request_digest(
        prompt=PromptRef(id=PROMPT.id, version=2, template=PROMPT.template),
        tier=Tier.SMALL,
        schema_name="R",
        inputs={},
        attachments=(),
    )
    other_tier = request_digest(
        prompt=PROMPT, tier=Tier.MID, schema_name="R", inputs={}, attachments=()
    )
    assert len({fixture_key(base), fixture_key(bumped), fixture_key(other_tier)}) == 3


def test_fixture_layout(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = fixture_path(tmp_path, "receipts", "receipts.extract", "abc")
    assert path == tmp_path / "receipts" / "receipts.extract" / "abc.json"
