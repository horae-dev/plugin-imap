# SPDX-License-Identifier: Apache-2.0
"""Conservative date extraction, French and English."""

from datetime import date

import pytest

from horae_imap import dates

D = date


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Due 2026-11-15.", [D(2026, 11, 15)]),
        ("avant le 15/11/2026", [D(2026, 11, 15)]),
        ("by 15.11.2026 please", [D(2026, 11, 15)]),
        ("15-11-2026", [D(2026, 11, 15)]),
        ("le 15 novembre 2026", [D(2026, 11, 15)]),
        ("le 1er décembre 2026", [D(2026, 12, 1)]),
        ("le 3 février 2027", [D(2027, 2, 3)]),
        ("le 3 FEVRIER 2027", [D(2027, 2, 3)]),
        ("le 12 août 2026", [D(2026, 8, 12)]),
        ("le 4 janv. 2027", [D(2027, 1, 4)]),
        ("15 November 2026", [D(2026, 11, 15)]),
        ("November 15, 2026", [D(2026, 11, 15)]),
        ("Nov 15th 2026", [D(2026, 11, 15)]),
        ("the 22nd of March 2027", [D(2027, 3, 22)]),
        ("2 mars 2027", [D(2027, 3, 2)]),
        ("2 Mar 2027", [D(2027, 3, 2)]),
    ],
)
def test_dates_with_a_year(text: str, expected: list[date]) -> None:
    assert dates.extract(text) == expected


def test_month_first_numeric_is_opt_in() -> None:
    assert dates.extract("11/15/2026") == []  # month 15 does not exist: nothing guessed
    assert dates.extract("11/15/2026", order="mdy") == [D(2026, 11, 15)]
    assert dates.extract("03/04/2026") == [D(2026, 4, 3)]
    assert dates.extract("03/04/2026", order="mdy") == [D(2026, 3, 4)]


def test_a_date_without_a_year_needs_a_reference_day() -> None:
    assert dates.extract("le 15 novembre") == []
    assert dates.extract("November 15") == []
    reference = D(2026, 11, 2)
    assert dates.extract("le 15 novembre", reference=reference) == [D(2026, 11, 15)]
    assert dates.extract("November 15", reference=reference) == [D(2026, 11, 15)]
    # already past this year: the next one, never a past date
    assert dates.extract("le 1er octobre", reference=reference) == [D(2027, 10, 1)]
    assert dates.extract("29 février", reference=D(2027, 3, 1)) == [D(2028, 2, 29)]
    assert dates.extract("29 février", reference=D(2027, 12, 31)) == [D(2028, 2, 29)]


@pytest.mark.parametrize(
    "text",
    [
        "31/02/2026",  # not a real day
        "30 février 2026",
        "2026-13-01",
        "2026-11-31",
        "15/11/26",  # two-digit year: not guessed
        "15/11",  # no year in the numeric form
        "version 1.2.2026.3",
        "call 2026-11-15-12",
        "12345/11/2026",
        "I may arrive later",  # a month name alone
        "1 mars",  # no reference: nothing
        "in 3 days",
        "demain",
        "15/11/1850",
        "15 novembre 3000",
        "",
    ],
)
def test_nothing_is_guessed(text: str) -> None:
    assert dates.extract(text) == []


def test_order_of_appearance_dedupe_and_limit() -> None:
    text = "le 20 novembre 2026, puis 2026-11-05, encore 20/11/2026, et 1 décembre 2026"
    assert dates.extract(text) == [D(2026, 11, 20), D(2026, 11, 5), D(2026, 12, 1)]
    many = " ".join(f"2026-11-{n:02d}" for n in range(1, 12))
    assert len(dates.extract(many)) == 5
    assert len(dates.extract(many, limit=2)) == 2


def test_is_linear_on_hostile_text() -> None:
    hostile = "1" * 50_000 + " " + "nov " * 20_000
    assert dates.extract(hostile) == []
