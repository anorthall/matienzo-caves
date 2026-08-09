"""Corrections applied to parsed records.

The design question these tests pin down is *where a fix lives*. Overrides are
files applied to the record before it reaches the database, so a rebuild
reproduces them — and, critically, an override whose page has changed underneath
it is not applied at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from matienzo import overrides
from matienzo.models import ParsedSite
from matienzo.parse import parse_page

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def record() -> ParsedSite:
    return parse_page(FIXTURES / "0001.htm")


def write_override(directory: Path, record: ParsedSite, body: str) -> Path:
    path = directory / f"{record.site_number:04d}.toml"
    path.write_text(
        f"site = {record.site_number}\n"
        f'applies_to_sha256 = "{record.provenance.content_sha256}"\n'
        f'author = "test"\n\n{body}',
        encoding="utf-8",
    )
    return path


class TestSegments:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("title.name", ["title", "name"]),
            ("header.area_raw", ["header", "area_raw"]),
            ("header.coordinates[1].label", ["header", "coordinates", 1, "label"]),
            ("updated[0].attribution", ["updated", 0, "attribution"]),
        ],
    )
    def test_paths_split_correctly(self, path: str, expected: list[str | int]) -> None:
        assert overrides._segments(path) == expected


class TestApply:
    def test_a_scalar_field_is_corrected(self, record: ParsedSite, tmp_path: Path) -> None:
        write_override(
            tmp_path,
            record,
            '[[fix]]\nfield_path = "header.area_raw"\nvalue = "Riva Corrected"\n'
            'rationale = "test"\n',
        )
        loaded = overrides.load_all(tmp_path)
        corrected, outcome = overrides.apply(record, loaded[record.site_number])

        assert corrected.header is not None
        assert corrected.header.area_raw == "Riva Corrected"
        assert outcome is not None
        assert outcome.status is overrides.OverrideStatus.ACTIVE

    def test_a_nested_list_element_is_corrected(
        self, record: ParsedSite, tmp_path: Path
    ) -> None:
        write_override(
            tmp_path,
            record,
            '[[fix]]\nfield_path = "header.coordinates[0].label"\n'
            'value = "Main entrance"\nrationale = "test"\n',
        )
        loaded = overrides.load_all(tmp_path)
        corrected, _ = overrides.apply(record, loaded[record.site_number])

        assert corrected.header is not None
        assert corrected.header.coordinates[0].label == "Main entrance"

    def test_the_result_is_revalidated(self, record: ParsedSite, tmp_path: Path) -> None:
        """An override cannot introduce a shape the rest of the pipeline
        rejects — the same models validate it as validate parser output."""
        write_override(
            tmp_path,
            record,
            '[[fix]]\nfield_path = "site_number"\nvalue = "not a number"\n'
            'rationale = "test"\n',
        )
        loaded = overrides.load_all(tmp_path)
        with pytest.raises(Exception, match="site_number"):
            overrides.apply(record, loaded[record.site_number])

    def test_no_override_leaves_the_record_untouched(self, record: ParsedSite) -> None:
        result, outcome = overrides.apply(record, None)
        assert result is record
        assert outcome is None


class TestStaleness:
    def test_an_override_for_different_bytes_is_not_applied(
        self, record: ParsedSite, tmp_path: Path
    ) -> None:
        """The whole reason overrides carry a hash. Re-applying a correction to
        changed source produces data that is wrong and that nothing flags."""
        path = tmp_path / "0001.toml"
        path.write_text(
            "site = 1\n"
            'applies_to_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"\n'
            'author = "test"\n\n'
            '[[fix]]\nfield_path = "header.area_raw"\nvalue = "Wrong"\nrationale = "x"\n',
            encoding="utf-8",
        )
        loaded = overrides.load_all(tmp_path)
        result, outcome = overrides.apply(record, loaded[1])

        assert outcome is not None
        assert outcome.status is overrides.OverrideStatus.STALE
        assert result.header is not None
        assert result.header.area_raw == "Riva", "the record must be left alone"

    def test_an_override_the_parser_now_satisfies_is_superseded(
        self, record: ParsedSite, tmp_path: Path
    ) -> None:
        """Stops override files accumulating for ever as the parser improves."""
        write_override(
            tmp_path,
            record,
            '[[fix]]\nfield_path = "header.area_raw"\nvalue = "Riva"\nrationale = "test"\n',
        )
        loaded = overrides.load_all(tmp_path)
        _, outcome = overrides.apply(record, loaded[record.site_number])

        assert outcome is not None
        assert outcome.status is overrides.OverrideStatus.SUPERSEDED


class TestLoading:
    def test_an_empty_directory_yields_nothing(self, tmp_path: Path) -> None:
        assert overrides.load_all(tmp_path) == {}

    def test_a_missing_directory_yields_nothing(self, tmp_path: Path) -> None:
        assert overrides.load_all(tmp_path / "nope") == {}

    def test_metadata_is_kept(self, record: ParsedSite, tmp_path: Path) -> None:
        path = write_override(
            tmp_path,
            record,
            '[[fix]]\nfield_path = "header.area_raw"\nvalue = "X"\n'
            'rationale = "because the page is wrong"\n',
        )
        override = overrides.load_all(tmp_path)[1]
        assert override.author == "test"
        assert override.path == path
        assert override.fixes[0].rationale == "because the page is wrong"
