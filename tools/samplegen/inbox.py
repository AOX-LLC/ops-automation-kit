"""Generate the inbox sample set: 28 .eml files, a business profile and the triage answer key."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import yaml

from tools.samplegen.common import answer_key_dir, samples_dir, write_json, write_text

SPEC_PATH = Path(__file__).parent / "specs" / "inbox.yaml"
MESSAGE_ID_DOMAIN = "kit.example"
WORKFLOW = "inbox"


@dataclass(frozen=True)
class Message:
    number: int
    slug: str
    from_name: str
    from_addr: str
    subject: str
    date: datetime
    language: str
    body: str
    in_reply_to: str | None
    reply_to: str | None
    labels: dict[str, Any]

    @property
    def file_name(self) -> str:
        return f"m{self.number:02d}.eml"

    @property
    def message_id(self) -> str:
        return f"<m{self.number:02d}.{self.slug}@{MESSAGE_ID_DOMAIN}>"


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        spec: dict[str, Any] = yaml.safe_load(handle)
    return spec


def parse_messages(raw_messages: list[dict[str, Any]]) -> list[Message]:
    return [
        Message(
            number=number,
            slug=raw["slug"],
            from_name=raw["from_name"],
            from_addr=raw["from_addr"],
            subject=raw["subject"],
            date=datetime.fromisoformat(raw["date"]),
            language=raw["language"],
            body=raw["body"],
            in_reply_to=raw.get("in_reply_to"),
            reply_to=raw.get("reply_to"),
            labels=raw["labels"],
        )
        for number, raw in enumerate(raw_messages, start=1)
    ]


def build_email(message: Message, recipient: str) -> EmailMessage:
    email = EmailMessage(policy=SMTP)
    email["Date"] = format_datetime(message.date)
    email["Message-ID"] = message.message_id
    email["From"] = f"{message.from_name} <{message.from_addr}>"
    if message.reply_to:
        email["Reply-To"] = message.reply_to
    email["To"] = recipient
    email["Subject"] = message.subject
    if message.in_reply_to:
        email["In-Reply-To"] = message.in_reply_to
        email["References"] = message.in_reply_to
    email.set_content(message.body + "\n", charset="utf-8", cte="8bit")
    return email


def render_profile(business: dict[str, Any]) -> str:
    lines = [
        f"# {business['name']}",
        "",
        business["summary"],
        "",
        f"- Based in: {business['town']}",
        f"- Owner: {business['owner']}",
        f"- Office phone: {business['phone']}",
        f"- Email: office@{business['domain']}",
        "",
        "## Hours",
        "",
        *[f"- {hours}" for hours in business["hours"]],
        "",
        "## Service area",
        "",
        *[f"- {town}" for town in business["service_area"]],
        "",
        business["service_area_note"],
        "",
        "## Price list",
        "",
        *[f"- {entry['item']}: {entry['price']}" for entry in business["prices"]],
        "",
        "## Policies",
        "",
        *[f"- {policy['name']}: {policy['text']}" for policy in business["policies"]],
        "",
    ]
    return "\n".join(lines)


def triage_entry(message: Message) -> dict[str, Any]:
    return {
        "file": message.file_name,
        "message_id": message.message_id,
        "language": message.language,
        "injection": False,
        "reply_to_differs": False,
        **message.labels,
    }


def generate(out_root: Path) -> None:
    spec = load_spec()
    business = spec["business"]
    recipient = f"office@{business['domain']}"
    messages = parse_messages(spec["messages"])

    messages_dir = samples_dir(out_root, WORKFLOW) / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    for message in messages:
        (messages_dir / message.file_name).write_bytes(build_email(message, recipient).as_bytes())

    write_text(samples_dir(out_root, WORKFLOW) / "business_profile.md", render_profile(business))
    write_json(
        answer_key_dir(out_root, WORKFLOW) / "triage.json",
        [triage_entry(message) for message in messages],
    )
