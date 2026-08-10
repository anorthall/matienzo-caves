"""Decoding is the foundation everything else rests on, so it is over-tested."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from matienzo import config
from matienzo.anomaly import AnomalyCode, AnomalyRecorder, Severity
from matienzo.decode import (
    MOJIBAKE_RE,
    Encoding,
    _report_encoding_anomalies,
    collapse_whitespace,
    decode_bytes,
    normalise_source,
    read_document,
    unescape,
)

# Site number -> a string that must appear in the decoded text. Each entry is a
# file where a naive decode produces something plausible but wrong.
DECODE_EXPECTATIONS: dict[int, str] = {
    255: "<B>Riaño</B>",  # genuine UTF-8; latin-1 would give "RiaÃ±o"
    788: "<B>Bosmartín</B>",  # ditto
    100: "Muñoz Fernández",  # ditto, in a citation
    2414: "first pitch — a head-first",  # UTF-8 em dash
    39: "CroquisZonaExploración-Marzo",  # CP1252 \xf3
    672: "Speleo-Club Cántabro",  # CP1252 \xe1
    1232: "‘exciting’!",  # CP1252 \x91/\x92 — undefined in ISO-8859-1
    1551: "wasn’t fenced off",  # CP1252 \x92
}

ENCODING_EXPECTATIONS: dict[int, Encoding] = {
    255: Encoding.UTF8,
    788: Encoding.UTF8,
    100: Encoding.UTF8,
    2414: Encoding.UTF8,
    39: Encoding.CP1252,
    672: Encoding.CP1252,
    1232: Encoding.CP1252,
    1551: Encoding.CP1252,
}


@pytest.mark.parametrize(("site", "expected"), DECODE_EXPECTATIONS.items())
def test_decodes_to_correct_text(
    fixture_path: Callable[[int], Path], site: int, expected: str
) -> None:
    doc = read_document(fixture_path(site))
    assert expected in doc.text


@pytest.mark.parametrize(("site", "expected"), ENCODING_EXPECTATIONS.items())
def test_takes_the_expected_decode_path(
    fixture_path: Callable[[int], Path], site: int, expected: Encoding
) -> None:
    doc = read_document(fixture_path(site))
    assert doc.encoding is expected


def test_cp1252_fallback_is_recorded_but_not_an_error(
    fixture_path: Callable[[int], Path],
) -> None:
    recorder = AnomalyRecorder()
    read_document(fixture_path(1232), recorder=recorder)
    assert recorder.has(AnomalyCode.CP1252_FALLBACK)
    assert recorder.count(Severity.ERROR) == 0


def test_latin1_would_corrupt_the_utf8_files(fixture_path: Callable[[int], Path]) -> None:
    """Guards the decision to try UTF-8 first rather than trusting the declared
    charset. 0255 declares nothing; decoding it as latin-1 silently mojibakes it."""
    raw = fixture_path(255).read_bytes()
    assert "Riaño" in raw.decode("utf-8")
    assert "RiaÃ±o" in raw.decode("latin-1")


def test_latin1_would_lose_the_cp1252_files(fixture_path: Callable[[int], Path]) -> None:
    """Guards the choice of CP1252 over latin-1 for the fallback. 0x91/0x92 are
    curly quotes in CP1252 and C1 control characters in ISO-8859-1."""
    raw = fixture_path(1232).read_bytes()
    assert "‘exciting’" in raw.decode("cp1252")
    assert "\x91exciting\x92" in raw.decode("latin-1")


def test_decode_bytes_never_raises() -> None:
    """CP1252 leaves 0x81, 0x8D, 0x8F, 0x90 and 0x9D undefined, so it is not a
    sufficient fallback on its own; latin-1 backstops it."""
    text, encoding = decode_bytes(bytes(range(256)))
    assert encoding is Encoding.LATIN1
    assert len(text) == 256


def test_cp1252_is_preferred_over_latin1_when_it_can_decode() -> None:
    text, encoding = decode_bytes(b"wasn\x92t")
    assert (text, encoding) == ("wasn’t", Encoding.CP1252)


def test_latin1_fallback_is_an_error_worth_a_human_look() -> None:
    recorder = AnomalyRecorder()
    _report_encoding_anomalies("ok", Encoding.LATIN1, recorder)
    assert recorder.count(Severity.ERROR) == 1


def test_normalise_source_folds_crlf_and_composes_accents() -> None:
    decomposed = "Riaño"  # n + combining tilde
    assert normalise_source(decomposed) == "Riaño"
    assert normalise_source("a\r\nb\rc") == "a\nb\nc"


def test_normalise_source_strips_c0_controls_but_keeps_newlines_and_tabs() -> None:
    assert normalise_source("a\x00b\x07c\td\ne") == "abc\td\ne"


class TestUnescape:
    def test_resolves_named_and_numeric_entities(self) -> None:
        assert unescape("Ria&ntilde;o &amp; Fern&aacute;ndez &#176;") == "Riaño & Fernández °"

    def test_output_is_nfc(self) -> None:
        assert unescape("Rian&#771;o") == "Riaño"

    def test_unescaping_before_splitting_is_what_keeps_citations_intact(self) -> None:
        """The footer's reference list is `;`-separated and `&aacute;` also ends
        in `;`. Splitting first is how `Fernández` becomes `Fern` + `aacute`."""
        raw = "Fern&aacute;ndez Guti&eacute;rrez et al, 1966; Corrin J S, 1981"
        assert [c.strip() for c in unescape(raw).split(";")] == [
            "Fernández Gutiérrez et al, 1966",
            "Corrin J S, 1981",
        ]
        assert len(raw.split(";")) == 4  # the wrong order gives four fragments

    def test_reports_an_unterminated_entity(self) -> None:
        recorder = AnomalyRecorder()
        unescape("20th November&nbsp", recorder=recorder)
        assert recorder.has(AnomalyCode.UNTERMINATED_ENTITY)

    def test_a_bare_ampersand_in_a_url_is_not_reported(self) -> None:
        recorder = AnomalyRecorder()
        unescape("Leaflet-latest.html?center=1&zoom=16&map_type=GV_HYBRID", recorder=recorder)
        assert not recorder.has(AnomalyCode.UNKNOWN_ENTITY)


def test_collapse_whitespace_unwraps_the_legacy_hard_wrap() -> None:
    """Source lines wrap at ~72 chars; those newlines are an editor artefact."""
    assert collapse_whitespace("A shaft with a\nwindow into a\n  second shaft.") == (
        "A shaft with a window into a second shaft."
    )


def test_mojibake_regex_matches_double_encoding_but_not_real_spanish() -> None:
    assert MOJIBAKE_RE.search("RiaÃ±o")
    assert MOJIBAKE_RE.search("wasnâ€™t")
    assert not MOJIBAKE_RE.search("Riaño Bosmartín Cántabro Muñoz")


def test_provenance_is_recorded(fixture_path: Callable[[int], Path]) -> None:
    path = fixture_path(255)
    doc = read_document(path)
    assert doc.site_number == 255
    assert doc.byte_size == path.stat().st_size
    assert len(doc.content_sha256) == 64
    assert doc.used_fallback is False


@pytest.mark.slow
class TestWholeCorpus:
    """Measured against the live `data/pages/` tree, so a re-scrape that changes the
    encoding profile shows up as a failing test rather than silent drift."""

    def test_encoding_breakdown_matches_the_survey(self) -> None:
        ascii_only = utf8 = cp1252 = 0
        for path in config.iter_page_paths():
            doc = read_document(path)
            if doc.text.isascii():
                ascii_only += 1
            elif doc.encoding is Encoding.UTF8:
                utf8 += 1
            else:
                cp1252 += 1

        assert (ascii_only, utf8, cp1252) == (5507, 32, 18)

    def test_no_page_contains_mojibake_or_replacement_characters(self) -> None:
        offenders = []
        for path in config.iter_page_paths():
            recorder = AnomalyRecorder()
            read_document(path, recorder=recorder)
            if recorder.count(Severity.ERROR):
                offenders.append(path.stem)
        assert offenders == []

    def test_every_page_decodes_and_has_a_site_number_matching_its_filename(self) -> None:
        paths = config.iter_page_paths()
        assert len(paths) == 5557
        for path in paths:
            doc = read_document(path)
            assert doc.site_number == int(path.stem)
            assert doc.text.strip()
