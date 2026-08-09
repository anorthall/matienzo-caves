"""Parse the `Updated …` line into dates.

Present on 2,615 of the 5,557 pages; its absence is normal and costs no
confidence. The line reads::

    Updated 9th November, 6th December 2003; 28th February 2008;
            24th July, 12th September 2019

Two structural rules, neither stated anywhere on the site:

**Semicolons group, commas list.** A group is one editing session or one year's
worth of edits.

**Within a group, the month and year are written only once, at the end.** So the
9th of November above is November *2003*, inherited from the group's last date.
Reading each token independently would date it to the current year, and the
update history is the only per-page chronology the corpus has — a page whose
history is silently wrong is worse than one with none.

The rest is tolerance for how the line has actually been typed over 25 years:
month names misspelled (`Febrary`) or split (`O ctober`), ordinals present or
absent, day-first or month-first, numeric `1/2/2011`, ranges
`13th-15th November 2019`, and trailing attributions `(Simon Cornhill)`.
"""

from __future__ import annotations

import calendar
import datetime as dt
import itertools
import re

from matienzo.anomaly import AnomalyCode, AnomalyRecorder, ConfidenceScorer
from matienzo.htmlutil import strip_tags
from matienzo.models import DatePrecision, UpdateDate

UPDATED_PREFIX_RE = re.compile(r"^\s*updated\s*:?\s*", re.IGNORECASE)

#: Trailing `(Simon Cornhill)` naming who made the edit.
ATTRIBUTION_RE = re.compile(r"\(([^)]*)\)\s*$")

#: `13th-15th November 2019` and `3rd - 7th September`.
RANGE_RE = re.compile(r"^(\d{1,2})\s*(?:st|nd|rd|th)?\s*[-–]\s*(\d{1,2})\s*(?:st|nd|rd|th)?\b")

#: `1/2/2011`. Day-first: this is a British corpus and `26/10/2001` settles it.
NUMERIC_RE = re.compile(r"^(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})$")

DAY_RE = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)\b|^\s*(\d{1,2})\s+(?=[A-Za-z])")
YEAR_RE = re.compile(r"\b((?:1[89]|20)\d{2})\b")

#: A token that is nothing but a year. Written when a comma slipped in before
#: it — `19th September, 2012` — so it is not a date of its own; it is the year
#: the preceding tokens in the group are missing.
BARE_YEAR_RE = re.compile(r"^\(?\s*((?:1[89]|20)\d{2})\s*\)?$")

#: A missing comma between two dates: `27th October 15th November 2001`. Splits
#: where a month name is followed directly by another ordinal day. Written to
#: require a letter before the space so it cannot break `13th-15th November`.
RUN_ON_RE = re.compile(r"(?<=[A-Za-z])\s+(?=\d{1,2}\s*(?:st|nd|rd|th)\b)")

MONTHS: dict[str, int] = {
    name.lower(): number for number, name in enumerate(calendar.month_name) if name
}
MONTHS |= {name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}

#: Misspellings and typographic damage seen in the corpus. Kept as an explicit
#: table rather than fuzzy-matched: a spell-corrector that turns `March` into
#: `May` because someone typed `Marh` would be silently wrong, and there are few
#: enough of these to name them.
MONTH_TYPOS: dict[str, str] = {
    "febrary": "february",
    "febuary": "february",
    "febraury": "february",
    "januray": "january",
    "octber": "october",
    "septmber": "september",
    "aprl": "april",
    "novemebr": "november",
    "decemeber": "december",
}


def parse_updated(
    raw_html: str, recorder: AnomalyRecorder, confidence: ConfidenceScorer
) -> list[UpdateDate]:
    """Parse the update line's markup into dated entries, oldest first."""
    text = UPDATED_PREFIX_RE.sub("", strip_tags(raw_html)).strip()
    if not text:
        return []

    groups = text.split(";")
    results: list[list[UpdateDate]] = [[] for _ in groups]

    # Groups are walked backwards so that one written without a year at all
    # (`… 2006; 15th January; 12th February … 2007`) can take the year from the
    # group that follows it. The entries are marked `inferred_year`, because
    # this is a reading of the list's chronological order rather than something
    # the page states.
    trailing_year: int | None = None
    for index in reversed(range(len(groups))):
        parsed = _parse_group(groups[index], index, recorder, confidence, trailing_year)
        results[index] = parsed
        for entry in parsed:
            if entry.date is not None and not entry.inferred_year:
                trailing_year = entry.date.year
                break

    return [entry for group in results for entry in group]


def _parse_group(
    group: str,
    group_index: int,
    recorder: AnomalyRecorder,
    confidence: ConfidenceScorer,
    trailing_year: int | None = None,
) -> list[UpdateDate]:
    """Parse one semicolon-delimited group, back-filling month and year.

    Tokens are resolved right to left because that is the direction the
    information flows: the last token carries the month and year for all of them.
    """
    tokens = [piece for token in group.split(",") for piece in _split_run_ons(token) if piece]
    if not tokens:
        return []

    parsed: list[UpdateDate] = []
    month_context: int | None = None
    year_context: int | None = trailing_year

    for token in reversed(tokens):
        if match := BARE_YEAR_RE.match(token):
            # Supplies the year for earlier tokens without being a date itself.
            year_context = int(match.group(1))
            continue

        entry = _parse_token(token, group_index, month_context, year_context, recorder)
        if entry is None:
            confidence.deduct("updated", 0.05, f"unparsed date {token[:30]!r}")
            continue
        if entry.date is not None:
            month_context = entry.date.month
            year_context = entry.date.year
        parsed.append(entry)

    return list(reversed(parsed))


def _split_run_ons(token: str) -> list[str]:
    """Split a token that runs two dates together without a comma.

    A split is only taken when what precedes it is *already* a complete date —
    an ordinal day and a month. Without that check the rule fires on
    month-first dates too, turning `November 6th 2003` into `November` plus
    `6th 2003` and losing both.
    """
    parts = [p.strip() for p in RUN_ON_RE.split(token) if p.strip()]
    if len(parts) < 2:
        return parts

    tokens: list[str] = []
    pending = parts[0]
    for part in parts[1:]:
        if DAY_RE.search(pending) and _find_month(pending) is not None:
            tokens.append(pending)
            pending = part
        else:
            pending = f"{pending} {part}"
    tokens.append(pending)
    return tokens


def _parse_token(
    token: str,
    group_index: int,
    month_context: int | None,
    year_context: int | None,
    recorder: AnomalyRecorder,
) -> UpdateDate | None:
    raw = token
    attribution = None
    if match := ATTRIBUTION_RE.search(token):
        attribution = match.group(1).strip()
        token = token[: match.start()].strip()

    entry = UpdateDate(raw=raw, group_index=group_index, attribution=attribution)

    if match := NUMERIC_RE.match(token):
        day, month, year = (int(g) for g in match.groups())
        entry.date = _make_date(_expand_year(year), month, day)
        entry.precision = DatePrecision.DAY
        return entry if entry.date else _unparsed(entry, raw, recorder)

    month = _find_month(token)
    year_match = YEAR_RE.search(token)
    year = int(year_match.group(1)) if year_match else year_context
    entry.inferred_year = year_match is None and year_context is not None

    if month is None:
        month = month_context
        entry.inferred_month = month_context is not None
    if month is None or year is None:
        return _unparsed(entry, raw, recorder)

    if range_match := RANGE_RE.match(token):
        start, end = int(range_match.group(1)), int(range_match.group(2))
        entry.date = _make_date(year, month, start)
        entry.end_date = _make_date(year, month, end)
        entry.precision = DatePrecision.RANGE
        return entry if entry.date else _unparsed(entry, raw, recorder)

    day_match = DAY_RE.search(token)
    if day_match is None:
        entry.date = _make_date(year, month, 1)
        entry.precision = DatePrecision.MONTH
        return entry if entry.date else _unparsed(entry, raw, recorder)

    day = int(day_match.group(1) or day_match.group(2))
    entry.date = _make_date(year, month, day)
    entry.precision = DatePrecision.DAY
    return entry if entry.date else _unparsed(entry, raw, recorder)


def _unparsed(entry: UpdateDate, raw: str, recorder: AnomalyRecorder) -> UpdateDate | None:
    recorder.add(
        AnomalyCode.UPDATED_DATE_UNPARSED,
        f"cannot read a date from {raw[:40]!r}",
        field_path="updated",
    )
    return entry if entry.date else None


def _find_month(token: str) -> int | None:
    """Month number from a token, tolerating case, spacing damage and typos.

    Whole words are tried first, then adjacent pairs joined together, because
    `26th O ctober 2015` is in the corpus. The two passes must stay in that
    order: a single pass over space-tolerant runs turns `5th November` into
    `thNovember` and matches nothing at all.
    """
    words = re.findall(r"[A-Za-z]+", token)
    for word in words:
        if (number := _lookup(word)) is not None:
            return number
    for first, second in itertools.pairwise(words):
        if (number := _lookup(first + second)) is not None:
            return number
    return None


def _lookup(word: str) -> int | None:
    candidate = word.lower()
    return MONTHS.get(MONTH_TYPOS.get(candidate, candidate))


#: Two-digit years pivot here, matching `parse.body`. Nothing in the update
#: history predates 1998, but a blanket `+2000` would silently turn a typo like
#: `1/2/98` into 2098 rather than failing visibly.
TWO_DIGIT_YEAR_PIVOT = 50


def _expand_year(year: int) -> int:
    if year >= 100:
        return year
    return year + (2000 if year < TWO_DIGIT_YEAR_PIVOT else 1900)


def _make_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None
