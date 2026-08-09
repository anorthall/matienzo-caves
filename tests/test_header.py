"""Header parsing: area, coordinates, and the measurement model.

The measurement tests carry most of the weight here. `Length` is prose on 61
pages, and that prose encodes cave-system membership — the thing a naive
`float | None` column throws away.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from matienzo import config
from matienzo.anomaly import SECTIONS, AnomalyCode, AnomalyRecorder, ConfidenceScorer
from matienzo.decode import read_document
from matienzo.models import CoordSystem, Header, Modifier, ProseRelation, Quantity, QuantityKind
from matienzo.parse.header import parse_header
from matienzo.parse.segment import segment


def _header(path: Path) -> tuple[Header, AnomalyRecorder]:
    recorder = AnomalyRecorder()
    segments = segment(read_document(path, recorder=recorder), recorder)
    assert segments.header is not None
    return parse_header(segments.header.text, recorder, ConfidenceScorer(SECTIONS)), recorder


@pytest.fixture
def header(fixture_path: Callable[[int], Path]) -> Callable[[int], Header]:
    def _run(site: int) -> Header:
        return _header(fixture_path(site))[0]

    return _run


def _quantity(header: Header, label: str) -> Quantity:
    matches = [q for q in header.quantities if q.label == label]
    assert matches, f"no {label} quantity"
    return matches[0]


class TestArea:
    def test_area_is_the_first_bolded_run(self, header: Callable[[int], Header]) -> None:
        assert header(1).area_raw == "Riva"

    def test_accented_area_survives_decoding(self, header: Callable[[int], Header]) -> None:
        assert header(105).area_raw == "Riaño"

    def test_boilerplate_is_not_mistaken_for_an_area(self, header: Callable[[int], Header]) -> None:
        assert "Area position" not in (header(1).area_raw or "")


class TestCoordinates:
    def test_archetype(self, header: Callable[[int], Header]) -> None:
        (coordinate,) = header(1).coordinates
        assert (coordinate.easting, coordinate.northing) == (453898, 4794091)
        assert coordinate.zone == "30T"
        assert coordinate.datum == "ETRS89"
        assert coordinate.accuracy_code == "U"
        assert coordinate.altitude_m == 365.0

    def test_latlon_is_derived(self, header: Callable[[int], Header]) -> None:
        (coordinate,) = header(1).coordinates
        assert coordinate.latitude == pytest.approx(43.32, abs=0.05)
        assert coordinate.longitude == pytest.approx(-3.57, abs=0.05)

    def test_multi_entrance_page_keeps_both_with_their_own_altitudes(
        self, header: Callable[[int], Header]
    ) -> None:
        """Altitude belongs to the entrance, not the site — 1930's two entrances
        are 16 m apart vertically."""
        coordinates = header(1930).coordinates
        assert len(coordinates) == 2
        assert [c.altitude_m for c in coordinates] == [219.0, 203.0]
        assert coordinates[1].entrance_site_number == 5451

    def test_leading_zero_easting_normalises(self, header: Callable[[int], Header]) -> None:
        """0478 pads its easting to seven digits; read literally it would land
        the site hundreds of kilometres away."""
        eastings = [c.easting for c in header(478).coordinates]
        assert all(400_000 < e < 520_000 for e in eastings if e)

    def test_placeholder_coordinates_are_kept_not_dropped(
        self, fixture_path: Callable[[int], Path]
    ) -> None:
        """`30T 04- 47-` means "nobody wrote it down", which is a more useful
        fact than a missing row."""
        header, recorder = _header(fixture_path(2674))
        assert recorder.has(AnomalyCode.COORD_PLACEHOLDER)
        assert all(c.system is CoordSystem.PLACEHOLDER for c in header.coordinates)

    def test_legacy_vn_grid_reference(self, fixture_path: Callable[[int], Path]) -> None:
        header, recorder = _header(fixture_path(892))
        assert header.coordinates
        if any(c.system is CoordSystem.VN_GRID for c in header.coordinates):
            assert recorder.has(AnomalyCode.COORD_LEGACY_VN)


class TestNumericMeasurements:
    def test_plain_value(self, header: Callable[[int], Header]) -> None:
        length = _quantity(header(1), "length")
        assert (length.kind, length.value_m, length.modifier) == (
            QuantityKind.NUMERIC,
            94.0,
            Modifier.EXACT,
        )

    def test_decimal(self, header: Callable[[int], Header]) -> None:
        assert _quantity(header(484), "depth").value_m == 58.6

    def test_negative_depth(self, header: Callable[[int], Header]) -> None:
        assert _quantity(header(713), "depth").value_m == -43.0

    def test_circa(self, header: Callable[[int], Header]) -> None:
        depth = _quantity(header(253), "depth")
        assert depth.modifier in {Modifier.CIRCA, Modifier.UNCERTAIN, Modifier.EXACT}

    def test_at_least(self, header: Callable[[int], Header]) -> None:
        length = _quantity(header(86), "length")
        assert (length.value_m, length.modifier) == (290.0, Modifier.AT_LEAST)

    def test_compound_value_keeps_its_parts(self, header: Callable[[int], Header]) -> None:
        """`Length 5 & 5m` is two separate holes recorded in one field."""
        length = _quantity(header(325), "length")
        assert length.kind is QuantityKind.COMPOUND
        assert length.parts == [5.0, 5.0]
        assert length.value_m == 10.0

    def test_true_range(self, header: Callable[[int], Header]) -> None:
        """1930: `Vertical Range from 232m (Top Tip) to 130m (downstream …)`."""
        vertical = _quantity(header(1930), "vertical_range")
        assert vertical.kind is QuantityKind.RANGE
        assert (vertical.min_m, vertical.max_m) == (130.0, 232.0)
        assert vertical.value_m == 102.0

    def test_altitude_is_not_a_site_quantity(self, header: Callable[[int], Header]) -> None:
        assert [q for q in header(1).quantities if q.label == "altitude"] == []


class TestProseMeasurements:
    """The reason `Quantity` has a `kind` instead of being `float | None`."""

    def test_system_membership_is_captured(self, fixture_path: Callable[[int], Path]) -> None:
        header, recorder = _header(fixture_path(81))
        length = _quantity(header, "length")
        assert length.kind is QuantityKind.PROSE
        assert length.prose is not None
        assert length.prose.relation is ProseRelation.PART_OF_SYSTEM
        assert length.prose.system_name == "Four Valleys System"
        assert 107 in length.prose.target_sites
        assert recorder.has(AnomalyCode.MEASURE_PROSE)

    def test_spanish_system_name(self, header: Callable[[int], Header]) -> None:
        prose = _quantity(header(105), "length").prose
        assert prose is not None
        assert prose.system_name == "Sistema de Cuatro Valles"
        assert prose.relation is ProseRelation.TRAVERSE_OF

    def test_inclusion_in_another_site(self, header: Callable[[int], Header]) -> None:
        prose = _quantity(header(16), "length").prose
        assert prose is not None
        assert prose.relation is ProseRelation.INCLUDED_IN
        assert prose.target_sites == [246]

    def test_length_belonging_to_another_site(self, header: Callable[[int], Header]) -> None:
        """`(870m added to Risco)` — the number is real but is not this site's."""
        prose = _quantity(header(24), "length").prose
        assert prose is not None
        assert prose.relation is ProseRelation.ADDED_TO
        assert prose.extra_value_m == 870.0

    def test_raw_text_is_always_preserved(self, header: Callable[[int], Header]) -> None:
        assert "Four Valleys System" in _quantity(header(81), "length").raw

    def test_prose_value_does_not_swallow_the_boilerplate(
        self, header: Callable[[int], Header]
    ) -> None:
        assert "Area position" not in _quantity(header(16), "length").raw


@pytest.fixture(scope="module")
def headers() -> list[tuple[int, Header, AnomalyRecorder]]:
    """Every non-stub page's header, parsed once and shared."""
    out = []
    for path in config.iter_page_paths():
        recorder = AnomalyRecorder()
        segments = segment(read_document(path, recorder=recorder), recorder)
        if segments.header is None:
            continue
        parsed = parse_header(segments.header.text, recorder, ConfidenceScorer(SECTIONS))
        out.append((segments.site_number, parsed, recorder))
    return out


@pytest.mark.slow
class TestWholeCorpus:
    def test_every_non_stub_page_has_a_header(
        self, headers: list[tuple[int, Header, AnomalyRecorder]]
    ) -> None:
        assert len(headers) == 5554

    def test_area_coverage(self, headers: list[tuple[int, Header, AnomalyRecorder]]) -> None:
        with_area = sum(1 for _, h, _ in headers if h.area_raw)
        assert with_area / len(headers) >= 0.995

    def test_coordinate_coverage(self, headers: list[tuple[int, Header, AnomalyRecorder]]) -> None:
        with_coords = sum(1 for _, h, _ in headers if h.coordinates)
        assert 0.97 <= with_coords / len(headers) <= 0.995

    def test_numeric_length_coverage_matches_the_survey(
        self, headers: list[tuple[int, Header, AnomalyRecorder]]
    ) -> None:
        numeric = sum(
            1
            for _, h, _ in headers
            for q in h.quantities
            if q.label == "length" and q.kind is QuantityKind.NUMERIC
        )
        assert 0.60 <= numeric / len(headers) <= 0.70

    def test_only_one_coordinate_is_geographically_implausible(
        self, headers: list[tuple[int, Header, AnomalyRecorder]]
    ) -> None:
        """4968's northing is 96 km north of every other site — an upstream typo.
        Pinned exactly so a new one fails rather than hiding behind it."""
        offenders = [n for n, _, r in headers if r.has(AnomalyCode.COORD_OUT_OF_RANGE)]
        assert offenders == [4968]

    def test_every_utm_coordinate_that_converted_is_in_cantabria(
        self, headers: list[tuple[int, Header, AnomalyRecorder]]
    ) -> None:
        for site_number, header, _ in headers:
            for coordinate in header.coordinates:
                if coordinate.latitude is None:
                    continue
                assert 43.0 <= coordinate.latitude <= 43.6, site_number
                assert -3.9 <= coordinate.longitude <= -3.2, site_number

    def test_prose_lengths_all_yield_a_relation_or_a_target(
        self, headers: list[tuple[int, Header, AnomalyRecorder]]
    ) -> None:
        """A prose measurement that produces neither a relation nor a target is
        data we silently lost."""
        useless = [
            (n, q.raw)
            for n, h, _ in headers
            for q in h.quantities
            if q.kind is QuantityKind.PROSE
            and q.prose is not None
            and q.prose.relation is ProseRelation.UNPARSED
            and not q.prose.target_sites
            and not q.prose.system_name
        ]
        assert len(useless) <= 2, useless
