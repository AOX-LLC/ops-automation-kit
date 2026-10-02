"""Receipts sample set: rendered receipt images, a bank statement CSV and the answer keys.

Truth lives in specs/receipts.yaml. The RNG stream "receipts" only drives skew and noise,
so editing the spec never changes how a given receipt is distorted beyond its own draws.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any

import yaml
from PIL import Image, ImageDraw, ImageFont

from tools.samplegen.common import (
    answer_key_dir,
    rng,
    samples_dir,
    write_json,
    write_text,
)

SPEC_PATH = Path(__file__).parent / "specs" / "receipts.yaml"
FONT_DIR = Path(__file__).parent / "fonts"
THERMAL_FONT = FONT_DIR / "CourierPrime-Regular.ttf"
LETTERHEAD_FONT = FONT_DIR / "CutiveMono-Regular.ttf"

PDF_FIXED_TIME = datetime(2026, 9, 1, tzinfo=UTC)
MAX_SKEW_DEGREES = 3.0
THERMAL_COLUMNS = 32
THERMAL_WIDTH = 320
DESCRIPTION_COLUMNS = 22
LETTERHEAD_WIDTH = 760
DESK_GRAY = 226
SPECKLE_DENSITY = 0.004

# Bank posting that lands within this many days of the receipt date counts as "matched".
MAX_MATCHED_LAG_DAYS = 3

TARGET_MIX = {
    "matched": 21,
    "amount_mismatch": 3,
    "date_drift": 2,
    "missing_in_bank": 3,
    "duplicate_receipt": 1,
    "duplicate_charge": 2,
    "unreceipted_charge": 3,
    "out_of_scope": 8,
}
TARGET_LAGGED_MATCHES = 8

RULES = {
    "matched": "Receipt and charge agree to the cent and the charge posts 0-3 days after the "
    "receipt date.",
    "date_drift": "Amounts agree but the charge posts more than 3 days after the receipt date.",
    "amount_mismatch": "Any difference in cents between receipt total and charge, even when the "
    "posting lag is within 3 days. Takes precedence over date_drift.",
    "missing_in_bank": "A receipt with no charge on the statement.",
    "unreceipted_charge": "A charge with no receipt.",
    "duplicate_charge": "The same descriptor and amount posted a second time. The first posting "
    "keeps its own status; only the repeat is labeled.",
    "duplicate_receipt": "The same receipt submitted a second time, re-rendered with different "
    "skew so the bytes differ. The original keeps its own status (matched when it agrees with "
    "its charge); only the resubmission is labeled, with a null bank_reference.",
    "out_of_scope": "Deposits and transfers. Never expected to have a receipt.",
    "delta_cents": "abs(bank amount) - abs(receipt total) in cents; null when either side is "
    "absent.",
    "delta_days": "bank posted_date - receipt date in days; null when either side is absent.",
    "amounts": "All amounts in the answer keys are integer cents. In the CSV, debits are "
    "negative and a refund posts as a positive credit.",
}


@dataclass(frozen=True)
class Card:
    brand: str
    last4: str


@dataclass(frozen=True)
class Vendor:
    name: str
    domain: str
    address: str
    phone: str
    descriptor: str
    style: str
    tax_bp: int


@dataclass(frozen=True)
class LineItem:
    description: str
    quantity: int
    unit_cents: int

    @property
    def line_cents(self) -> int:
        return self.quantity * self.unit_cents


@dataclass(frozen=True)
class Receipt:
    id: str
    vendor: Vendor
    receipt_date: date
    card: Card
    number: str
    items: tuple[LineItem, ...]
    tip_cents: int
    fmt: str
    low_contrast: bool
    outcome: str
    lag_days: int
    bank_delta_cents: int
    duplicate_charge_after_days: int | None
    duplicate_of: str | None

    @property
    def filename(self) -> str:
        return f"{self.id}.{self.fmt}"

    @property
    def subtotal_cents(self) -> int:
        return sum(item.line_cents for item in self.items)

    @property
    def tax_cents(self) -> int:
        subtotal = self.subtotal_cents
        rounded = (abs(subtotal) * self.vendor.tax_bp + 5000) // 10000
        return rounded if subtotal >= 0 else -rounded

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + self.tax_cents + self.tip_cents


@dataclass(frozen=True)
class BankEntry:
    posted: date
    description: str
    amount_cents: int
    status: str
    receipt: Receipt | None


@dataclass(frozen=True)
class Spec:
    business: str
    opening_balance_cents: int
    cards: tuple[Card, ...]
    receipts: tuple[Receipt, ...]
    unreceipted_charges: tuple[dict[str, Any], ...]
    other_rows: tuple[dict[str, Any], ...]


# ---------------------------------------------------------------- spec loading


def _build_receipt(
    raw: dict[str, Any], vendors: dict[str, Vendor], cards: tuple[Card, ...]
) -> Receipt:
    return Receipt(
        id=raw["id"],
        vendor=vendors[raw["vendor"]],
        receipt_date=raw["date"],
        card=cards[raw["card"]],
        number=raw["number"],
        items=tuple(LineItem(d, q, u) for d, q, u in raw["items"]),
        tip_cents=raw.get("tip_cents", 0),
        fmt=raw["fmt"],
        low_contrast=raw.get("low_contrast", False),
        outcome=raw["outcome"],
        lag_days=raw.get("lag_days", 0),
        bank_delta_cents=raw.get("bank_delta_cents", 0),
        duplicate_charge_after_days=raw.get("duplicate_charge_after_days"),
        duplicate_of=None,
    )


def load_spec(path: Path = SPEC_PATH) -> Spec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    vendors = {key: Vendor(**fields) for key, fields in sorted(raw["vendors"].items())}
    cards = tuple(Card(**card) for card in raw["cards"])
    receipts: dict[str, Receipt] = {}
    for entry in raw["receipts"]:
        source_id = entry.get("duplicate_of")
        if source_id is None:
            receipts[entry["id"]] = _build_receipt(entry, vendors, cards)
        else:
            receipts[entry["id"]] = replace(
                receipts[source_id],
                id=entry["id"],
                fmt=entry["fmt"],
                outcome=entry["outcome"],
                duplicate_of=source_id,
            )
    return Spec(
        business=raw["business"],
        opening_balance_cents=raw["statement"]["opening_balance_cents"],
        cards=cards,
        receipts=tuple(receipts[key] for key in sorted(receipts)),
        unreceipted_charges=tuple(raw["unreceipted_charges"]),
        other_rows=tuple(raw["other_rows"]),
    )


# ---------------------------------------------------------------- formatting


def format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _card_label(card: Card) -> str:
    return f"{card.brand} ****{card.last4}"


def _tax_label(vendor: Vendor) -> str:
    return f"TAX {vendor.tax_bp / 100:.2f}%"


# ---------------------------------------------------------------- rendering


def _load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    # BASIC layout keeps glyph placement independent of whether libraqm is installed.
    return ImageFont.truetype(str(path), size, layout_engine=ImageFont.Layout.BASIC)


def _palette(receipt: Receipt) -> tuple[int, int]:
    """Return (paper, ink) gray levels."""
    return (190, 120) if receipt.low_contrast else (249, 28)


def _thermal_lines(receipt: Receipt) -> list[str]:
    vendor = receipt.vendor
    cols = THERMAL_COLUMNS
    rule = "-" * cols
    lines = [
        vendor.name.upper().center(cols),
        f"{vendor.address}, Quillbrook".center(cols),
        f"Tel {vendor.phone}".center(cols),
        vendor.domain.center(cols),
        rule,
        f"{'REFUND' if receipt.total_cents < 0 else 'SALE':<8}#{receipt.number}"[:cols],
        receipt.receipt_date.strftime("%Y-%m-%d"),
        rule,
    ]
    for item in receipt.items:
        if len(item.description) > DESCRIPTION_COLUMNS:
            raise ValueError(f"thermal description too long: {item.description!r}")
        lines.append(_item_line(item.description, item.line_cents, cols))
        if item.quantity > 1:
            lines.append(f"  {item.quantity} @ {format_money(item.unit_cents)}")
    lines.append(rule)
    lines.append(_money_line("SUBTOTAL", receipt.subtotal_cents, cols))
    if receipt.vendor.tax_bp:
        lines.append(_money_line(_tax_label(vendor), receipt.tax_cents, cols))
    if receipt.tip_cents:
        lines.append(_money_line("TIP", receipt.tip_cents, cols))
    lines.append(_money_line("TOTAL", receipt.total_cents, cols))
    lines += [rule, _card_label(receipt.card).center(cols), "THANK YOU".center(cols)]
    return lines


def _item_line(description: str, cents: int, width: int) -> str:
    return (
        f"{description:<{DESCRIPTION_COLUMNS}}{format_money(cents):>{width - DESCRIPTION_COLUMNS}}"
    )


def _money_line(label: str, cents: int, width: int) -> str:
    amount = format_money(cents)
    return f"{label}{amount:>{width - len(label)}}"


def _render_thermal(receipt: Receipt) -> Image.Image:
    paper, ink = _palette(receipt)
    font = _load_font(THERMAL_FONT, 16)
    line_height = 19
    lines = _thermal_lines(receipt)
    image = Image.new("L", (THERMAL_WIDTH, 36 + line_height * len(lines)), paper)
    draw = ImageDraw.Draw(image)
    left = (THERMAL_WIDTH - round(draw.textlength("M" * THERMAL_COLUMNS, font=font))) // 2
    for row, line in enumerate(lines):
        draw.text((left, 18 + row * line_height), line, font=font, fill=ink)
    return image


def _render_letterhead(receipt: Receipt) -> Image.Image:
    paper, ink = _palette(receipt)
    vendor = receipt.vendor
    title_font = _load_font(LETTERHEAD_FONT, 34)
    body_font = _load_font(LETTERHEAD_FONT, 19)
    margin, row_height = 52, 30
    right_edge = LETTERHEAD_WIDTH - margin
    height = 500 + row_height * len(receipt.items)
    image = Image.new("L", (LETTERHEAD_WIDTH, height), paper)
    draw = ImageDraw.Draw(image)

    draw.rectangle((0, 0, LETTERHEAD_WIDTH, 12), fill=ink)
    draw.text((margin, 40), vendor.name, font=title_font, fill=ink)
    draw.text(
        (margin, 88),
        f"{vendor.address}, Quillbrook ST 00000",
        font=body_font,
        fill=ink,
    )
    draw.text((margin, 114), f"{vendor.phone}   {vendor.domain}", font=body_font, fill=ink)
    draw.line((margin, 150, right_edge, 150), fill=ink, width=2)

    kind = "CREDIT NOTE" if receipt.total_cents < 0 else "RECEIPT"
    draw.text((margin, 170), f"{kind} NO. {receipt.number}", font=body_font, fill=ink)
    date_text = f"Date: {receipt.receipt_date:%Y-%m-%d}"
    draw.text(
        (right_edge - draw.textlength(date_text, font=body_font), 170),
        date_text,
        font=body_font,
        fill=ink,
    )

    qty_x, unit_x = 470, 600
    y = 220
    draw.text((margin, y), "Description", font=body_font, fill=ink)
    draw.text((qty_x, y), "Qty", font=body_font, fill=ink)
    _draw_right(draw, (unit_x, y), "Unit", body_font, ink)
    _draw_right(draw, (right_edge, y), "Amount", body_font, ink)
    draw.line((margin, y + 26, right_edge, y + 26), fill=ink, width=1)
    y += 40
    for item in receipt.items:
        draw.text((margin, y), item.description[:26], font=body_font, fill=ink)
        draw.text((qty_x, y), str(item.quantity), font=body_font, fill=ink)
        _draw_right(draw, (unit_x, y), format_money(item.unit_cents), body_font, ink)
        _draw_right(draw, (right_edge, y), format_money(item.line_cents), body_font, ink)
        y += row_height
    draw.line((margin, y, right_edge, y), fill=ink, width=1)

    y += 14
    totals = [("Subtotal", receipt.subtotal_cents)]
    if vendor.tax_bp:
        totals.append((f"Tax {vendor.tax_bp / 100:.2f}%", receipt.tax_cents))
    if receipt.tip_cents:
        totals.append(("Tip", receipt.tip_cents))
    totals.append(("TOTAL", receipt.total_cents))
    for label, cents in totals:
        draw.text((unit_x - 130, y), label, font=body_font, fill=ink)
        _draw_right(draw, (right_edge, y), format_money(cents), body_font, ink)
        y += row_height - 4
    draw.text((margin, y + 24), f"Paid by {_card_label(receipt.card)}", font=body_font, fill=ink)
    draw.text((margin, y + 54), "Thank you for your business.", font=body_font, fill=ink)
    return image.crop((0, 0, LETTERHEAD_WIDTH, y + 90))


def _draw_right(
    draw: ImageDraw.ImageDraw,
    anchor: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    ink: int,
) -> None:
    x, y = anchor
    draw.text((x - draw.textlength(text, font=font), y), text, font=font, fill=ink)


def _distort(image: Image.Image, source: Random) -> Image.Image:
    """Place the receipt on a desk, skew it by up to 3 degrees, then add sparse noise."""
    padded = Image.new("L", (image.width + 48, image.height + 48), DESK_GRAY)
    padded.paste(image, (24, 24))
    angle = source.uniform(-MAX_SKEW_DEGREES, MAX_SKEW_DEGREES)
    skewed = padded.rotate(
        angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=DESK_GRAY
    )
    _add_speckle(skewed, source)
    return skewed


def _add_speckle(image: Image.Image, source: Random) -> None:
    pixels = image.load()
    if pixels is None:
        raise RuntimeError("image has no pixel access")
    for _ in range(int(image.width * image.height * SPECKLE_DENSITY)):
        x, y = source.randrange(image.width), source.randrange(image.height)
        current = pixels[x, y]
        if not isinstance(current, int):
            raise TypeError("expected a grayscale image")
        pixels[x, y] = max(0, min(255, current + round(source.gauss(0, 22))))


def render_receipt(receipt: Receipt, source: Random) -> Image.Image:
    if receipt.vendor.style == "thermal":
        flat = _render_thermal(receipt)
    else:
        flat = _render_letterhead(receipt)
    return _distort(flat, source)


def save_image(image: Image.Image, path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".pdf":
        image.save(
            path,
            format="PDF",
            resolution=150.0,
            title=title,
            creationDate=PDF_FIXED_TIME.timetuple(),
            modDate=PDF_FIXED_TIME.timetuple(),
        )
    else:
        image.save(path, format="PNG", optimize=False)


# ---------------------------------------------------------------- bank statement


def _receipt_charge(receipt: Receipt) -> BankEntry:
    return BankEntry(
        posted=receipt.receipt_date + timedelta(days=receipt.lag_days),
        description=receipt.vendor.descriptor,
        amount_cents=-(receipt.total_cents + receipt.bank_delta_cents),
        status=receipt.outcome,
        receipt=receipt,
    )


def _repeat_charge(receipt: Receipt) -> BankEntry:
    first = _receipt_charge(receipt)
    if receipt.duplicate_charge_after_days is None:
        raise ValueError(f"{receipt.id} has no duplicate charge")
    return replace(
        first,
        posted=first.posted + timedelta(days=receipt.duplicate_charge_after_days),
        status="duplicate_charge",
        receipt=None,
    )


def build_bank_entries(spec: Spec) -> list[BankEntry]:
    entries: list[BankEntry] = []
    for receipt in spec.receipts:
        if receipt.outcome == "missing_in_bank" or receipt.duplicate_of:
            continue
        entries.append(_receipt_charge(receipt))
        if receipt.duplicate_charge_after_days is not None:
            entries.append(_repeat_charge(receipt))
    for row in spec.unreceipted_charges:
        entries.append(_plain_entry(row, "unreceipted_charge"))
    for row in spec.other_rows:
        entries.append(_plain_entry(row, "out_of_scope"))
    return sorted(entries, key=lambda entry: entry.posted)


def _plain_entry(row: dict[str, Any], status: str) -> BankEntry:
    return BankEntry(row["date"], row["description"], row["amount_cents"], status, None)


def reference_for(index: int) -> str:
    return f"STM-{index + 1:04d}"


def render_statement_csv(entries: list[BankEntry], opening_balance_cents: int) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["posted_date", "description", "amount", "balance", "reference"])
    balance = opening_balance_cents
    for index, entry in enumerate(entries):
        balance += entry.amount_cents
        writer.writerow(
            [
                entry.posted.isoformat(),
                entry.description,
                format_money(entry.amount_cents),
                format_money(balance),
                reference_for(index),
            ]
        )
    return buffer.getvalue()


# ---------------------------------------------------------------- answer keys


def receipts_answer_key(spec: Spec) -> dict[str, Any]:
    return {
        "business": spec.business,
        "cards": [{"brand": card.brand, "last4": card.last4} for card in spec.cards],
        "receipts": [_receipt_truth(receipt) for receipt in spec.receipts],
    }


def _receipt_truth(receipt: Receipt) -> dict[str, Any]:
    vendor = receipt.vendor
    return {
        "file": receipt.filename,
        "vendor": vendor.name,
        "vendor_address": f"{vendor.address}, Quillbrook",
        "vendor_phone": vendor.phone,
        "date": receipt.receipt_date.isoformat(),
        "receipt_number": receipt.number,
        "line_items": [
            {
                "description": item.description,
                "quantity": item.quantity,
                "unit_price_cents": item.unit_cents,
                "line_total_cents": item.line_cents,
            }
            for item in receipt.items
        ],
        "subtotal_cents": receipt.subtotal_cents,
        "tax_cents": receipt.tax_cents,
        "tip_cents": receipt.tip_cents,
        "total_cents": receipt.total_cents,
        "card_brand": receipt.card.brand,
        "card_last4": receipt.card.last4,
        "is_refund": receipt.total_cents < 0,
    }


def reconciliation_rows(spec: Spec, entries: list[BankEntry]) -> list[dict[str, Any]]:
    rows = [_bank_row(index, entry) for index, entry in enumerate(entries)]
    unlinked = [r for r in spec.receipts if r.outcome == "missing_in_bank" or r.duplicate_of]
    for receipt in unlinked:
        rows.append(_receipt_only_row(receipt))
    return rows


def _bank_row(index: int, entry: BankEntry) -> dict[str, Any]:
    receipt = entry.receipt
    return {
        "receipt": receipt.filename if receipt else None,
        "bank_reference": reference_for(index),
        "status": entry.status,
        "delta_cents": abs(entry.amount_cents) - abs(receipt.total_cents) if receipt else None,
        "delta_days": (entry.posted - receipt.receipt_date).days if receipt else None,
    }


def _receipt_only_row(receipt: Receipt) -> dict[str, Any]:
    return {
        "receipt": receipt.filename,
        "bank_reference": None,
        "status": receipt.outcome,
        "delta_cents": None,
        "delta_days": None,
    }


def count_statuses(rows: list[dict[str, Any]]) -> Counter[str]:
    return Counter(row["status"] for row in rows)


def check_target_mix(rows: list[dict[str, Any]]) -> None:
    counts = count_statuses(rows)
    if dict(counts) != TARGET_MIX:
        raise ValueError(f"status mix {dict(sorted(counts.items()))} != {TARGET_MIX}")
    lagged = sum(
        1
        for row in rows
        if row["status"] == "matched" and row["delta_days"] and row["delta_days"] > 0
    )
    if lagged != TARGET_LAGGED_MATCHES:
        raise ValueError(f"{lagged} lagged matches, expected {TARGET_LAGGED_MATCHES}")
    for row in rows:
        _check_row_rules(row)


def _check_row_rules(row: dict[str, Any]) -> None:
    status, days, cents = row["status"], row["delta_days"], row["delta_cents"]
    if status == "matched" and not (0 <= days <= MAX_MATCHED_LAG_DAYS and cents == 0):
        raise ValueError(f"row breaks the matched rule: {row}")
    if status == "date_drift" and not (days > MAX_MATCHED_LAG_DAYS and cents == 0):
        raise ValueError(f"row breaks the date_drift rule: {row}")
    if status == "amount_mismatch" and cents == 0:
        raise ValueError(f"row breaks the amount_mismatch rule: {row}")


# ---------------------------------------------------------------- entry point


def generate(out_root: Path) -> None:
    spec = load_spec()
    source = rng("receipts")
    inbox = samples_dir(out_root, "receipts") / "inbox"
    for receipt in spec.receipts:
        save_image(render_receipt(receipt, source), inbox / receipt.filename, receipt.id)

    entries = build_bank_entries(spec)
    rows = reconciliation_rows(spec, entries)
    check_target_mix(rows)

    bank_csv = render_statement_csv(entries, spec.opening_balance_cents)
    write_text(samples_dir(out_root, "receipts") / "bank" / "statement-2026-08.csv", bank_csv)
    keys = answer_key_dir(out_root, "receipts")
    write_json(keys / "receipts.json", receipts_answer_key(spec))
    write_json(keys / "reconciliation.json", {"rules": RULES, "rows": rows})
