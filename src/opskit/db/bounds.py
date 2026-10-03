"""Builders for the rules `core.enforce_bounds()` reads, and the SQL that attaches them.

A spec maps a column to a rule. The database refuses an insert, or an update that changes the
column, when the value breaks its rule. A column that did not change is never re-checked, so a
row stored before a bound existed keeps working for unrelated updates and is never rewritten.
A key `column.member` checks a member of a JSON column.

The migrations that attach bounds import this module, so keep its output stable: changing a rule
here changes what an old migration would build.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

type Rule = dict[str, Any]
type Spec = Mapping[str, Rule]


def text(max: int, *, min: int = 0, regex: str | None = None, nullable: bool = False) -> Rule:
    """A string of `min` to `max` characters, optionally in the form `regex` (a POSIX ARE)."""
    rule: Rule = {"kind": "text", "min": min, "max": max, "nullable": nullable}
    if regex is not None:
        rule["regex"] = regex
    return rule


def enum(*values: str, nullable: bool = False) -> Rule:
    return {"kind": "enum", "values": list(values), "nullable": nullable}


def number(min: float, max: float, *, nullable: bool = False) -> Rule:
    return {"kind": "number", "min": min, "max": max, "nullable": nullable}


def moment(
    *,
    after: str | None = None,
    before: str | None = None,
    min_s: int | None = None,
    max_s: int | None = None,
    nullable: bool = False,
) -> Rule:
    """A finite time. `after` and `before` are absolute (ISO 8601 with a zone); `min_s` and
    `max_s` are seconds from the database clock (negative is the past)."""
    rule: Rule = {"kind": "moment", "nullable": nullable}
    for name, value in (("after", after), ("before", before), ("min_s", min_s), ("max_s", max_s)):
        if value is not None:
            rule[name] = value
    return rule


def document(
    type: str,
    *,
    max_bytes: int,
    depth: int,
    items: int | None = None,
    nullable: bool = False,
) -> Rule:
    """A JSON `type` ('object' or 'array') of at most `max_bytes` (as Postgres prints it), with
    nothing nested more than `depth` levels below the top, and at most `items` array elements."""
    rule: Rule = {
        "kind": "document",
        "type": type,
        "max_bytes": max_bytes,
        "depth": depth,
        "nullable": nullable,
    }
    if items is not None:
        rule["items"] = items
    return rule


def json_text(type: str, *, max_bytes: int, depth: int, nullable: bool = False) -> Rule:
    """Like `document`, for a text column that holds JSON (the audit log stores it as text)."""
    return {
        **document(type, max_bytes=max_bytes, depth=depth, nullable=nullable),
        "kind": "json_text",
    }


def id_map(max_items: int, *, key_regex: str, value_regex: str, nullable: bool = False) -> Rule:
    """A JSON object of at most `max_items` entries, every key and every (string) value in form."""
    return {
        "kind": "id_map",
        "max_items": max_items,
        "key_regex": key_regex,
        "value_regex": value_regex,
        "nullable": nullable,
    }


def literal(value: object) -> str:
    """A SQL string literal holding `value` as JSON. Migrations run through SQLAlchemy's text(),
    which reads `:name` and `:300` as bind parameters, so a colon is always followed by a space."""
    return "'" + json.dumps(value, separators=(", ", ": "), sort_keys=True).replace("'", "''") + "'"


def attach_sql(table: str, name: str, spec: Spec, *, events: str = "INSERT OR UPDATE") -> str:
    """CREATE TRIGGER for `table` (schema-qualified), enabled even under replica mode."""
    return (
        f"CREATE TRIGGER {name} BEFORE {events} ON {table} FOR EACH ROW "
        f"EXECUTE FUNCTION core.enforce_bounds({literal(dict(spec))});\n"
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name};"
    )


def detach_sql(table: str, name: str) -> str:
    return f"DROP TRIGGER {name} ON {table};"


def report_sql(table: str, spec: Spec, key: Sequence[str]) -> str:
    """A DO block that raises one WARNING per table naming the rows already outside the spec:
    their keys and the column and kind of the problem, never the values (this repository is
    public and a value may be client data). It changes nothing."""
    keys = "ARRAY[" + ", ".join(f"'{column}'" for column in key) + "]"
    return f"""
        DO $report$
        DECLARE
            found text[];
        BEGIN
            SELECT coalesce(array_agg(v), '{{}}') INTO found
            FROM core.bounds_violations('{table}'::regclass, {literal(dict(spec))}, {keys}) v;
            IF cardinality(found) > 0 THEN
                RAISE WARNING
                    '{table}: % stored row(s) are outside the new bounds and are kept: %',
                    cardinality(found), found[1:50];
            END IF;
        END
        $report$;"""  # noqa: S608 - table and column names are migration constants
