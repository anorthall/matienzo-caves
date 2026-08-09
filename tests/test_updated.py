"""The update history: the only per-page chronology the corpus carries.

Most of the risk is in the back-fill. `9th November, 6th December 2003` means
the 9th of *November 2003*; reading each token independently silently dates it
to the current year and the page's history becomes fiction.
"""

from __future__ import annotations

import datetime as dt

import pytest

from matienzo import config
from matienzo.anomaly import SECTIONS, AnomalyCode, AnomalyRecorder, ConfidenceScorer
from matienzo.decode import read_document
from matienzo.models import DatePrecision, UpdateDate
from matienzo.parse.segment import segment
from matienzo.parse.updated import parse_updated


def parse(text: str) -> list[UpdateDate]:
    return parse_updated(text, AnomalyRecorder(), ConfidenceScorer(SECTIONS))


def dates(text: str) -> list[dt.date | None]:
    return [entry.date for entry in parse(text)]


class TestBackFill:
    def test_year_flows_backwards_within_a_group(self) -> None:
        assert dates("Updated 9th November, 6th December 2003") == [
            dt.date(2003, 11, 9),
            dt.date(2003, 12, 6),
        ]

    def test_month_and_year_both_flow_backwards(self) -> None:
        """`8th` alone inherits both March and 2010."""
        assert dates("Updated 16th January, 8th, 9th March, 24th June 2010") == [
            dt.date(2010, 1, 16),
            dt.date(2010, 3, 8),
            dt.date(2010, 3, 9),
            dt.date(2010, 6, 24),
        ]

    def test_inference_is_recorded(self) -> None:
        first, second = parse("Updated 9th November, 6th December 2003")
        assert first.inferred_year and not second.inferred_year

    def test_groups_are_independent(self) -> None:
        assert dates("Updated 28th February 2008; 12th September 2019") == [
            dt.date(2008, 2, 28),
            dt.date(2019, 9, 12),
        ]

    def test_group_with_no_year_takes_it_from_the_group_that_follows(self) -> None:
        """`… 2006; 15th January; 12th February 2007` — January belongs to 2007.
        Marked inferred, because it is a reading of the list's order rather than
        something the page states."""
        entries = parse("Updated 1st October 2006; 15th January; 12th February 2007")
        assert [e.date for e in entries] == [
            dt.date(2006, 10, 1),
            dt.date(2007, 1, 15),
            dt.date(2007, 2, 12),
        ]
        assert entries[1].inferred_year


class TestIrregularForms:
    def test_date_range(self) -> None:
        (entry,) = parse("Updated 13th-15th November 2019")
        assert entry.date == dt.date(2019, 11, 13)
        assert entry.end_date == dt.date(2019, 11, 15)
        assert entry.precision is DatePrecision.RANGE

    def test_attribution_is_split_off(self) -> None:
        (entry,) = parse("Updated 9th July 2024 (Simon Cornhill)")
        assert entry.date == dt.date(2024, 7, 9)
        assert entry.attribution == "Simon Cornhill"

    def test_numeric_date_is_read_day_first(self) -> None:
        """A British corpus: `26/10/2001` settles the ambiguity."""
        assert dates("Updated 1/2/2011") == [dt.date(2011, 2, 1)]

    def test_month_only(self) -> None:
        (entry,) = parse("Updated November 2015")
        assert entry.date == dt.date(2015, 11, 1)
        assert entry.precision is DatePrecision.MONTH

    def test_month_first_order(self) -> None:
        assert dates("Updated October 25th 2015") == [dt.date(2015, 10, 25)]

    def test_no_ordinal_suffix(self) -> None:
        assert dates("Updated 9 January 2010") == [dt.date(2010, 1, 9)]

    def test_lowercase_month(self) -> None:
        assert dates("Updated 4th september 2023") == [dt.date(2023, 9, 4)]

    @pytest.mark.parametrize("spelling", ["Febrary", "Febuary", "Febraury"])
    def test_month_typos(self, spelling: str) -> None:
        assert dates(f"Updated 2nd {spelling} 2013") == [dt.date(2013, 2, 2)]

    def test_space_inside_the_month_name(self) -> None:
        """`26th O ctober 2015` is really in the corpus."""
        assert dates("Updated 26th O ctober 2015") == [dt.date(2015, 10, 26)]

    def test_comma_before_a_bare_year(self) -> None:
        """`19th September, 2012` — the year is not a date of its own."""
        assert dates("Updated 19th September, 2012") == [dt.date(2012, 9, 19)]

    def test_missing_comma_between_two_dates(self) -> None:
        assert dates("Updated 27th October 15th November 2001") == [
            dt.date(2001, 10, 27),
            dt.date(2001, 11, 15),
        ]

    def test_month_first_date_is_not_split_as_a_run_on(self) -> None:
        """The run-on rule must not fire between a month and the day after it,
        or `November 6th 2003` becomes `November` plus `6th 2003`."""
        assert dates("Updated November 6th 2003") == [dt.date(2003, 11, 6)]

    def test_impossible_date_is_rejected_rather_than_clamped(self) -> None:
        entries = parse("Updated 31st February 2013")
        assert entries == [] or entries[0].date is None


class TestTagVariants:
    @pytest.mark.parametrize("tag", ["I", "i", "EM", "em"])
    def test_all_emphasis_tags(self, tag: str) -> None:
        assert dates(f"<{tag}>Updated 3rd May 2009</{tag}>") == [dt.date(2009, 5, 3)]

    def test_markup_inside_the_line_is_stripped(self) -> None:
        assert dates("Updated <a href='x.htm'>3rd May</a> 2009") == [dt.date(2009, 5, 3)]

    def test_empty_line_yields_nothing(self) -> None:
        assert parse("Updated") == []


@pytest.fixture(scope="module")
def histories() -> list[tuple[int, list[UpdateDate], AnomalyRecorder]]:
    out = []
    for path in config.iter_page_paths():
        recorder = AnomalyRecorder()
        segments = segment(read_document(path, recorder=recorder), recorder)
        if segments.updated is None:
            continue
        parsed = parse_updated(segments.updated.text, recorder, ConfidenceScorer(SECTIONS))
        out.append((segments.site_number, parsed, recorder))
    return out


@pytest.mark.slow
class TestWholeCorpus:
    def test_every_update_line_yields_at_least_one_date(
        self, histories: list[tuple[int, list[UpdateDate], AnomalyRecorder]]
    ) -> None:
        assert len(histories) == 2615
        empty = [n for n, entries, _ in histories if not entries]
        assert empty == []

    def test_date_yield(
        self, histories: list[tuple[int, list[UpdateDate], AnomalyRecorder]]
    ) -> None:
        assert sum(len(entries) for _, entries, _ in histories) == 7051

    def test_unparsed_tokens_are_rare(
        self, histories: list[tuple[int, list[UpdateDate], AnomalyRecorder]]
    ) -> None:
        total = sum(len(entries) for _, entries, _ in histories)
        failures = sum(
            1
            for _, _, recorder in histories
            for anomaly in recorder.items
            if anomaly.code is AnomalyCode.UPDATED_DATE_UNPARSED
        )
        assert failures / total < 0.005

    def test_all_dates_fall_in_a_plausible_range(
        self, histories: list[tuple[int, list[UpdateDate], AnomalyRecorder]]
    ) -> None:
        """Catches a back-fill that silently defaulted to the current year."""
        for site_number, entries, _ in histories:
            for entry in entries:
                if entry.date is None:
                    continue
                assert 1990 <= entry.date.year <= 2030, (site_number, entry.raw)

    def test_histories_are_almost_always_chronological(
        self, histories: list[tuple[int, list[UpdateDate], AnomalyRecorder]]
    ) -> None:
        """The cross-group year inheritance assumes the list runs forwards.
        If that assumption were wrong this count would be large, not 3."""
        out_of_order = [
            n
            for n, entries, _ in histories
            if (d := [e.date for e in entries if e.date]) != sorted(d)
        ]
        assert len(out_of_order) <= 5
