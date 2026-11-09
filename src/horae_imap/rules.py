# SPDX-License-Identifier: Apache-2.0
"""The rules: configuration (trusted) matched against a message (untrusted), no LLM.

A rule has a `name`, a `kind` (`request` or `date`) and matchers on the sender's address
(`from`), the subject (`subject`) and the `List-Id` (`list_id`). Each matcher is a list of
case-insensitive `fnmatch` patterns (`*@school.example.org`, `*inscription*`; `*`, `?` and
`[...]`); a field matches when any of its patterns does, and the rule matches when every field
it names does. At least one matcher is required, so no rule matches everything by accident.
Rules are tried in order and the first match wins: a message yields at most one item.

- `request`: the message asks for something. Its item always exists; its due date is the first
  date found in the subject and text that is not before the message's own day.
- `date`: the message announces a date. Its item exists only when a date is found.

Patterns are globs, not regular expressions, and run on fields cut to a few hundred
characters, so a hostile message cannot make a rule slow.
"""

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from horae_sdk import PluginError

from horae_imap import dates
from horae_imap.message import Message

MAX_RULES = 32
MAX_PATTERNS = 20
MAX_PATTERN = 100
MATCH_CHARS = 300
"""Characters of a field a pattern is tried against."""
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
FIELDS = ("from", "subject", "list_id")
KEYS = frozenset({"name", "kind", "task_prefix", *FIELDS})
Kind = Literal["request", "date"]


@dataclass(frozen=True)
class Rule:
    name: str
    kind: Kind
    patterns: tuple[tuple[str, tuple[str, ...]], ...]
    """`(field, lower-case patterns)` for each matcher the rule names."""
    task_prefix: str


@dataclass(frozen=True)
class Item:
    kind: Kind
    rule: str
    task_prefix: str
    dates: tuple[date, ...]
    """Every date found (at most 5), in order of appearance."""
    due: date | None


def parse_rules(value: object) -> tuple[Rule, ...]:
    """The `rules` parameter, checked. Errors name the field, never a message."""
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) > MAX_RULES:
        raise PluginError(f"config_invalid: rules must be a list of at most {MAX_RULES}")
    rules: list[Rule] = []
    seen: set[str] = set()
    for position, spec in enumerate(value):
        where = f"config_invalid: rules[{position}]"
        if not isinstance(spec, Mapping):
            raise PluginError(f"{where} must be a mapping")
        unknown = set(spec) - KEYS
        if unknown:
            raise PluginError(f"{where}: unknown key {sorted(map(str, unknown))[0]!r}")
        name, kind = spec.get("name"), spec.get("kind")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise PluginError(f"{where}.name must be 1 to 40 of A-Z a-z 0-9 . _ -")
        if name in seen:
            raise PluginError(f"{where}.name is used twice")
        if kind not in ("request", "date"):
            raise PluginError(f"{where}.kind must be request or date")
        prefix = spec.get("task_prefix", "")
        if not isinstance(prefix, str) or len(prefix) > 40:
            raise PluginError(f"{where}.task_prefix must be a string of at most 40 characters")
        patterns: list[tuple[str, tuple[str, ...]]] = []
        for field in FIELDS:
            if field not in spec:
                continue
            given = spec[field]
            if (
                not isinstance(given, Sequence)
                or isinstance(given, str)
                or not 1 <= len(given) <= MAX_PATTERNS
                or not all(isinstance(p, str) and 1 <= len(p) <= MAX_PATTERN for p in given)
            ):
                raise PluginError(
                    f"{where}.{field} must list 1 to {MAX_PATTERNS} patterns "
                    f"of 1 to {MAX_PATTERN} characters"
                )
            patterns.append((field, tuple(p.lower() for p in given)))
        if not patterns:
            raise PluginError(f"{where} needs at least one of from, subject, list_id")
        seen.add(name)
        rules.append(Rule(name, kind, tuple(patterns), prefix))
    return tuple(rules)


def _field(message: Message, field: str) -> str:
    value = {"from": message.sender, "subject": message.subject, "list_id": message.list_id}
    return value[field][:MATCH_CHARS].lower()


def matches(rule: Rule, message: Message) -> bool:
    return all(
        any(fnmatch.fnmatchcase(_field(message, field), p) for p in patterns)
        for field, patterns in rule.patterns
    )


def evaluate(
    rules: Sequence[Rule], message: Message, *, order: str = "dmy", max_dates: int = 5
) -> Item | None:
    """The item the first matching rule makes of `message`, or `None`."""
    for rule in rules:
        if not matches(rule, message):
            continue
        found = dates.extract(
            f"{message.subject}\n{message.text}",
            reference=message.day,
            order=order,
            limit=max_dates,
        )
        if rule.kind == "date" and not found:
            return None
        upcoming = [d for d in found if message.day is None or d >= message.day]
        due = upcoming[0] if upcoming else None
        return Item(rule.kind, rule.name, rule.task_prefix, tuple(found), due)
    return None


def describe(item: Item) -> dict[str, Any]:
    """The item as returned data (no message content)."""
    return {
        "kind": item.kind,
        "rule": item.rule,
        "dates": [d.isoformat() for d in item.dates],
        "due": None if item.due is None else item.due.isoformat(),
    }
