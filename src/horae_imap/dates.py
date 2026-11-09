# SPDX-License-Identifier: Apache-2.0
"""Conservative date extraction from untrusted text, French and English.

Deterministic and clock-free: a date without a year is resolved against the message's own
`Date` header (the first such day on or after it, within a year), never against `now()`.

Recognised (everything else is ignored, so a wrong guess is never made):

- ISO `2026-11-15`;
- numeric with a four-digit year, day first by default (`15/11/2026`, `15.11.2026`,
  `15-11-2026`), or month first with `order="mdy"` (`11/15/2026`);
- a month name, French or English, abbreviated or not, accents optional: `15 novembre 2026`,
  `1er déc. 2026`, `15 November 2026`, `November 15, 2026`, `Nov 15th 2026`;
- the same without a year (`15 novembre`, `November 15`) when a reference day is known.

Only real calendar days in 1990 to 2100 are returned.
"""

import re
import unicodedata
from datetime import date

MAX_TEXT = 64 * 1024
"""Characters scanned: callers pass already capped text, this is the backstop."""
MIN_YEAR, MAX_YEAR = 1990, 2100

_MONTHS = {
    # French (accents folded away), full names then common abbreviations
    "janvier": 1,
    "janv": 1,
    "fevrier": 2,
    "fevr": 2,
    "mars": 3,
    "avril": 4,
    "avr": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "juil": 7,
    "aout": 8,
    "septembre": 9,
    "sept": 9,
    "octobre": 10,
    "oct": 10,
    "novembre": 11,
    "nov": 11,
    "decembre": 12,
    "dec": 12,
    # English
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MONTH = "(?P<month>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?"
_DAY = r"(?P<day>[0-3]?\d)(?:er|st|nd|rd|th)?"
_YEAR = r"(?P<year>(?:19|20|21)\d\d)"

ISO = re.compile(rf"(?<![\d-]){_YEAR}-(?P<month>[01]\d)-(?P<day>[0-3]\d)(?![\d-])")
NUMERIC = re.compile(
    rf"(?<![\d/.-])(?P<a>[0-3]?\d)(?P<sep>[/.-])(?P<b>[01]?\d)(?P=sep){_YEAR}(?![\d/-])(?!\.\d)"
)
DAY_MONTH = re.compile(rf"(?<!\d){_DAY}\s+(?:of\s+)?{_MONTH}(?:,?\s+{_YEAR}(?!\d))?")
MONTH_DAY = re.compile(rf"\b{_MONTH}\s+{_DAY}(?!\d)(?:,?\s+{_YEAR}(?!\d))?")


def fold(text: str) -> str:
    """Lower case, accents and compatibility forms removed (`Décembre` is `decembre`)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _real(year: int, month: int, day: int) -> date | None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _without_year(month: int, day: int, reference: date | None) -> date | None:
    """The first `month`/`day` on or after `reference`, within a year; `None` without one."""
    if reference is None:
        return None
    for year in (reference.year, reference.year + 1):
        found = _real(year, month, day)
        if found is not None and found >= reference:
            return found
    return None


def extract(
    text: str,
    *,
    reference: date | None = None,
    order: str = "dmy",
    limit: int = 5,
) -> list[date]:
    """The distinct dates found in `text`, in order of appearance (at most `limit`).

    `reference` (the message's own day) resolves dates written without a year; `order` is
    `"dmy"` or `"mdy"` for the numeric form."""
    folded = fold(text[:MAX_TEXT])
    found: list[tuple[int, date]] = []
    for match in ISO.finditer(folded):
        day = _real(int(match["year"]), int(match["month"]), int(match["day"]))
        if day is not None:
            found.append((match.start(), day))
    for match in NUMERIC.finditer(folded):
        first, second = int(match["a"]), int(match["b"])
        day_number, month_number = (first, second) if order == "dmy" else (second, first)
        day = _real(int(match["year"]), month_number, day_number)
        if day is not None:
            found.append((match.start(), day))
    for pattern, day_group in ((DAY_MONTH, "day"), (MONTH_DAY, "day")):
        for match in pattern.finditer(folded):
            month_number = _MONTHS[match["month"]]
            day_number = int(match[day_group])
            if match["year"] is not None:
                day = _real(int(match["year"]), month_number, day_number)
            else:
                day = _without_year(month_number, day_number, reference)
            if day is not None:
                found.append((match.start(), day))
    result: list[date] = []
    for _, day in sorted(found, key=lambda pair: pair[0]):
        if day not in result:
            result.append(day)
        if len(result) >= limit:
            break
    return result
