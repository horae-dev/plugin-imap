# SPDX-License-Identifier: Apache-2.0
"""Rule configuration and matching: deterministic, glob patterns, first match wins."""

from datetime import date
from typing import Any

import pytest
from horae_sdk import PluginError

from horae_imap import message, rules


def msg(
    sender: str = "teacher@school.example.org",
    subject: str = "Forms",
    text: str = "",
    list_id: str = "",
    day: date | None = date(2026, 11, 2),
) -> message.Message:
    return message.Message(1, sender, subject, list_id, "", day, text)


RULES = rules.parse_rules(
    [
        {
            "name": "school",
            "kind": "request",
            "from": ["*@school.example.org"],
            "subject": ["*autorisation*", "*form*"],
            "task_prefix": "Reply: ",
        },
        {"name": "news", "kind": "date", "list_id": ["news.example.org"]},
    ]
)


def test_a_request_matches_on_every_named_field() -> None:
    item = rules.evaluate(RULES, msg(text="Merci de répondre avant le 15/11/2026."))
    assert item is not None
    assert (item.kind, item.rule, item.task_prefix) == ("request", "school", "Reply: ")
    assert item.dates == (date(2026, 11, 15),)
    assert item.due == date(2026, 11, 15)
    assert rules.describe(item) == {
        "kind": "request",
        "rule": "school",
        "dates": ["2026-11-15"],
        "due": "2026-11-15",
    }


def test_a_request_without_a_date_has_no_due() -> None:
    item = rules.evaluate(RULES, msg(subject="Autorisation de sortie"))
    assert item is not None
    assert item.due is None
    assert item.dates == ()


def test_patterns_are_case_insensitive_and_all_fields_must_match() -> None:
    assert (
        rules.evaluate(RULES, msg(sender="Teacher@SCHOOL.example.org", subject="FORMS")) is not None
    )
    assert rules.evaluate(RULES, msg(sender="teacher@elsewhere.example.org")) is None
    assert rules.evaluate(RULES, msg(subject="Lunch")) is None


def test_a_date_rule_needs_a_date() -> None:
    assert rules.evaluate(RULES, msg(sender="x@example.org", list_id="news.example.org")) is None
    item = rules.evaluate(
        RULES,
        msg(sender="x@example.org", list_id="news.example.org", text="Fête le 12 décembre"),
    )
    assert item is not None
    assert (item.kind, item.dates, item.due) == ("date", (date(2026, 12, 12),), date(2026, 12, 12))


def test_the_first_matching_rule_wins() -> None:
    both = rules.parse_rules(
        [
            {"name": "a", "kind": "date", "subject": ["*"]},
            {"name": "b", "kind": "request", "subject": ["*"]},
        ]
    )
    item = rules.evaluate(both, msg(subject="no date"))
    assert item is None  # rule a matched first and, needing a date, ends the search


def test_past_dates_are_listed_but_never_due() -> None:
    item = rules.evaluate(RULES, msg(text="Le 1 octobre 2026 puis le 20/11/2026"))
    assert item is not None
    assert item.dates == (date(2026, 10, 1), date(2026, 11, 20))
    assert item.due == date(2026, 11, 20)
    unknown_day = rules.evaluate(RULES, msg(text="le 1 octobre 2026", day=None))
    assert unknown_day is not None
    assert unknown_day.due == date(2026, 10, 1)


def test_a_yearless_date_uses_the_messages_own_day() -> None:
    item = rules.evaluate(RULES, msg(text="avant le 15 novembre"))
    assert item is not None
    assert item.due == date(2026, 11, 15)
    item = rules.evaluate(RULES, msg(text="avant le 15 novembre", day=None))
    assert item is not None
    assert item.due is None


def test_matching_is_bounded_on_hostile_fields() -> None:
    hostile = rules.parse_rules(
        [{"name": "h", "kind": "request", "subject": ["*a*a*a*a*a*a*a*a*b"]}]
    )
    assert rules.evaluate(hostile, msg(subject="a" * 100_000)) is None


def test_no_rules_no_items() -> None:
    assert rules.evaluate((), msg()) is None
    assert rules.parse_rules(None) == ()


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("nope", "list"),
        ([1], "mapping"),
        ([{"name": "a", "kind": "request"}], "at least one"),
        ([{"name": "a b", "kind": "request", "from": ["*"]}], "name"),
        ([{"name": "a", "kind": "todo", "from": ["*"]}], "kind"),
        ([{"name": "a", "kind": "request", "from": "*"}], "from"),
        ([{"name": "a", "kind": "request", "from": []}], "from"),
        ([{"name": "a", "kind": "request", "from": [""]}], "from"),
        ([{"name": "a", "kind": "request", "from": ["x" * 101]}], "from"),
        ([{"name": "a", "kind": "request", "from": ["*"], "regex": "x"}], "unknown key"),
        ([{"name": "a", "kind": "request", "from": ["*"], "task_prefix": "x" * 41}], "task_prefix"),
        (
            [
                {"name": "a", "kind": "request", "from": ["*"]},
                {"name": "a", "kind": "date", "from": ["*"]},
            ],
            "twice",
        ),
        ([{"name": f"r{n}", "kind": "request", "from": ["*"]} for n in range(33)], "at most"),
    ],
)
def test_bad_rules_are_refused_by_name(value: Any, needle: str) -> None:
    with pytest.raises(PluginError, match=needle):
        rules.parse_rules(value)
