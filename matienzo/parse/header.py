"""Parse the `<SMALL>` info line: area, coordinates, and measurements.

The info line looks regular and is not. Three things drive the design:

**Fields are scanned by label, never by position.** There are 25 distinct field
orderings across the corpus, several pages repeat a label, and `1930` interleaves
two full coordinate-plus-altitude groups before it gets to `Length`. Any rule of
the form "the second `<BR>` group holds Length and Depth" is wrong somewhere.

**Measurements are not always numbers.** About 56 pages write prose into
`Length`, and that prose is *data*: `included in the Four Valleys System: see
<a href="0107.htm">Cueva Hoyuca</a>` records cave-system membership and a
cross-reference. Modelling the field as `float | None` would throw both away and
silently understate how many sites have a known length. See `models.Quantity`.

**Altitude belongs to the entrance, not the site.** Multi-entrance pages give
each entrance its own coordinate *and* its own altitude, so altitude is parsed
into `Coordinate`, not into the measurement list.
"""

from __future__ import annotations

import re

import utm

from matienzo.anomaly import AnomalyCode, AnomalyRecorder, ConfidenceScorer
from matienzo.decode import unescape
from matienzo.htmlutil import strip_tags
from matienzo.models import (
    Coordinate,
    CoordSystem,
    Header,
    Modifier,
    ProseQuantity,
    ProseRelation,
    Quantity,
    QuantityKind,
)

#: `<B>Label</B> value`. Tag case is mixed and the colon is sometimes *inside*
#: the bold tag (`<B>Vertical range:</B>`), so both are absorbed here.
LABEL_RE = re.compile(r"<b\b[^>]*>\s*(?P<label>[^<]{1,40}?)\s*:?\s*</b\s*>\s*:?", re.IGNORECASE)

#: Canonical names for the measurement labels actually seen. Anything else is
#: recorded verbatim and flagged rather than silently dropped.
MEASUREMENT_LABELS: dict[str, str] = {
    "length": "length",
    "depth": "depth",
    "altitude": "altitude",
    "alt.": "altitude",
    "alt": "altitude",
    "height": "height",
    "height range": "height_range",
    "vertical range": "vertical_range",
    "vertical  range": "vertical_range",
}

#: A UTM easting/northing. Eastings are usually six digits but `0478` and `1930`
#: pad theirs to seven with a leading zero, so both widths must be accepted —
#: and the value normalised, or the coordinate lands 400 km away.
UTM_RE = re.compile(r"\b(?P<zone>\d{2}[A-Z])\s+(?P<easting>0?\d{6})\s+(?P<northing>\d{7})\b")

#: Pre-ETRS89 Spanish grid references, and the placeholder forms for a
#: coordinate that was never recorded.
VN_RE = re.compile(r"\bVN\s?(?P<digits>[\d?]{4,8})\b")
COORD_PLACEHOLDER_RE = re.compile(r"\b\d{2}[A-Z]\s+(?:\?{1,2}|0?\d*-)\s*(?:\?{1,2}|\d*-)?")

DATUM_RE = re.compile(r"Datum\s*:\s*(?P<datum>[A-Za-z0-9]+)", re.IGNORECASE)
ACCURACY_RE = re.compile(r"Accuracy\s+code\s*:\s*(?P<code>[A-Z])\b", re.IGNORECASE)
ALTITUDE_RE = re.compile(r"\bAlt(?:itude|\.)?\s*:?\s*(?P<value>c?\.?\s*\d+(?:\.\d+)?)\s*m", re.I)

#: A label that introduces a named entrance on a multi-entrance page, e.g.
#: `Top entrance 30T …` or `Bottom entrance #5451 @ 30T …`.
ENTRANCE_LABEL_RE = re.compile(
    r"(?P<label>(?:[A-Z][A-Za-z]*\s+)?(?:entrance|end|entrances)\b[^0-9]*?)"
    r"(?:#(?P<site>\d{4}))?\s*@?\s*$",
    re.IGNORECASE,
)

#: Numeric measurement values, with their qualifiers. `<` and `up to` are upper
#: bounds; `c` is an estimate; a trailing `+` means "at least"; `?` means the
#: recorder was unsure. A bare `?m` has no number and falls through to UNKNOWN.
VALUE_RE = re.compile(
    r"^\s*(?P<atmost><|up\s+to\s+)?\s*(?P<circa>c\.?\s*)?(?P<sign>[+-])?\s*"
    r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>m\b)?\s*(?P<plus>\+)?\s*(?P<query>\?)?",
    re.IGNORECASE,
)

#: A value that states only that it is unknown: `?m`, `?`, `???m`.
UNKNOWN_VALUE_RE = re.compile(r"^\s*\?+\s*m?\s*$", re.IGNORECASE)
COMPOUND_RE = re.compile(
    r"^\s*(?P<first>\d+(?:\.\d+)?)\s*m?\s*&\s*(?P<second>\d+(?:\.\d+)?)\s*m?\s*(?P<query>\?)?",
    re.IGNORECASE,
)
RANGE_RE = re.compile(
    r"from\s+(?P<high>\d+(?:\.\d+)?)\s*m.*?\bto\s+(?P<low>\d+(?:\.\d+)?)\s*m",
    re.IGNORECASE | re.DOTALL,
)

#: How prose measurements phrase themselves. Ordered: the first match wins, so
#: the more specific patterns come first.
PROSE_PATTERNS: tuple[tuple[re.Pattern[str], ProseRelation], ...] = (
    (re.compile(r"\btraverse\s+length\b", re.IGNORECASE), ProseRelation.TRAVERSE_OF),
    (
        re.compile(r"\b(?:part|included)\s+(?:of|in|with)\s+(?:the\s+)?[\w\s]*?system\b", re.I),
        ProseRelation.PART_OF_SYSTEM,
    ),
    (
        re.compile(r"\bSistema\b", re.IGNORECASE),
        ProseRelation.PART_OF_SYSTEM,
    ),
    (re.compile(r"\badded\s+to\b", re.IGNORECASE), ProseRelation.ADDED_TO),
    (re.compile(r"\bincluded\b", re.IGNORECASE), ProseRelation.INCLUDED_IN),
    (re.compile(r"\bsee\b", re.IGNORECASE), ProseRelation.SEE_OTHER),
)

#: A cave-system name. Anchored on the keyword rather than scanned leftwards
#: from it, so `included in the South Vega System` yields `South Vega System`
#: rather than the whole clause.
SYSTEM_NAME_RE = re.compile(
    r"(?:Sistema\s+de\s+(?:la\s+|los\s+|las\s+)?[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)*"
    r"|(?:[A-Z][\w'’-]*\s+){1,3}System)",
)
SITE_LINK_RE = re.compile(r"href\s*=\s*[\"']?(?:\.\./)?(?:descrip/)?(\d{4})[-\w]*\.htm", re.I)
SITE_TEXT_RE = re.compile(r"\b(?:site|sites)?\s*\(?(\d{4})\)?\b")
EMBEDDED_M_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*m\b", re.IGNORECASE)

#: Bounds for zone 30T over the Matienzo depression, set generously around the
#: measured spread (easting 441,614–461,004; northing 4,791,601–4,805,285).
#: Exactly one coordinate in the corpus falls outside: `4968`, whose northing of
#: 4,901,207 puts it 96 km north of every other site — an upstream typo.
EASTING_RANGE = (400_000, 520_000)
NORTHING_RANGE = (4_750_000, 4_830_000)

#: The header's trailing boilerplate anchors. Measurement values run to the next
#: `<B>` label, and the boilerplate carries no labels, so without cutting here a
#: prose Length swallows `Area position : Site entrance in context : …`.
BOILERPLATE_START_RE = re.compile(
    r"<a\b[^>]*href\s*=\s*[\"']?#openModal|Area\s+position|Site\s+entrance\s+in\s+context",
    re.IGNORECASE,
)


def parse_header(raw_html: str, recorder: AnomalyRecorder, confidence: ConfidenceScorer) -> Header:
    """Turn the info line markup into a `Header`."""
    raw_html = _trim_boilerplate(raw_html)
    header = Header(raw=strip_tags(raw_html))

    header.area_raw = _parse_area(raw_html)
    if header.area_raw is None:
        recorder.add(AnomalyCode.AREA_MISSING, "no bolded area name", field_path="header")
        confidence.deduct("header", 0.3, "no area name")

    header.coordinates = _parse_coordinates(raw_html, recorder)
    if not header.coordinates:
        recorder.add(AnomalyCode.COORD_MISSING, "no coordinates", field_path="header")
        confidence.deduct("header", 0.15, "no coordinates")
    elif len(header.coordinates) > 1:
        recorder.add(
            AnomalyCode.COORD_MULTIPLE,
            f"{len(header.coordinates)} coordinates — a multi-entrance site",
            field_path="header",
        )

    header.quantities, header.field_order, header.duplicate_labels = _parse_quantities(
        raw_html, recorder, confidence
    )
    if header.duplicate_labels:
        recorder.add(
            AnomalyCode.MEASURE_DUPLICATE_LABEL,
            f"repeated measurement labels: {', '.join(header.duplicate_labels)}",
            field_path="header.quantities",
        )
    return header


def _trim_boilerplate(raw_html: str) -> str:
    """Drop the `Area position : … : Logbook search` anchor run.

    Segmentation keeps it because it is the header's terminator; field parsing
    must not see it, or every value that runs to the next `<B>` label picks it up.
    """
    match = BOILERPLATE_START_RE.search(raw_html)
    return raw_html[: match.start()] if match else raw_html


def _parse_area(raw_html: str) -> str | None:
    """The area name is the first bolded run in the info line.

    It is bolded exactly like the measurement labels, so the only thing
    distinguishing it is that it comes first and is not a known label.
    """
    for match in LABEL_RE.finditer(raw_html):
        label = unescape(match.group("label")).strip()
        if label.lower().rstrip(":") in MEASUREMENT_LABELS:
            return None  # a measurement came first, so there is no area
        return label or None
    return None


def _parse_coordinates(raw_html: str, recorder: AnomalyRecorder) -> list[Coordinate]:
    """Every coordinate on the page, with its entrance label and altitude.

    Multi-entrance pages describe each entrance in free text with no consistent
    markup, so the association between a coordinate, its label, its datum and its
    altitude is positional: label before, datum and altitude after, bounded by
    the next coordinate.
    """
    text = strip_tags(raw_html)
    matches = list(UTM_RE.finditer(text))
    coordinates: list[Coordinate] = []

    for index, match in enumerate(matches):
        following_start = match.end()
        following_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        following = text[following_start:following_end]
        preceding = (
            text[: match.start()] if index == 0 else text[matches[index - 1].end() : match.start()]
        )

        # A seven-digit easting is a six-digit one zero-padded. Failing to strip
        # that puts the site hundreds of kilometres away, which is why the range
        # check below exists.
        easting = int(match.group("easting"))
        northing = int(match.group("northing"))

        datum = DATUM_RE.search(following)
        accuracy = ACCURACY_RE.search(following)
        altitude = ALTITUDE_RE.search(following)
        label_match = ENTRANCE_LABEL_RE.search(preceding.strip())

        coordinate = Coordinate(
            raw=match.group(0),
            label=" ".join(label_match.group("label").split()) if label_match else None,
            entrance_site_number=int(label_match.group("site"))
            if label_match and label_match.group("site")
            else None,
            system=CoordSystem.UTM,
            zone=match.group("zone"),
            easting=easting,
            northing=northing,
            datum=datum.group("datum") if datum else None,
            accuracy_code=accuracy.group("code").upper() if accuracy else None,
            altitude_m=float(re.sub(r"[^\d.]", "", altitude.group("value"))) if altitude else None,
        )
        _add_latlon(coordinate, recorder)
        coordinates.append(coordinate)

    if not coordinates:
        coordinates.extend(_parse_fallback_coordinates(text, recorder))
    return coordinates


def _parse_fallback_coordinates(text: str, recorder: AnomalyRecorder) -> list[Coordinate]:
    """Legacy `VN########` grid references and unrecorded-coordinate placeholders.

    Kept rather than dropped: `VN????????` means "nobody wrote it down", which is
    a different and more useful fact than a missing row.
    """
    found: list[Coordinate] = []
    datum = DATUM_RE.search(text)
    accuracy = ACCURACY_RE.search(text)
    altitude = ALTITUDE_RE.search(text)

    for match in VN_RE.finditer(text):
        digits = match.group("digits")
        placeholder = "?" in digits
        recorder.add(
            AnomalyCode.COORD_PLACEHOLDER if placeholder else AnomalyCode.COORD_LEGACY_VN,
            f"legacy grid reference {match.group(0)!r}",
            field_path="header.coordinates",
        )
        found.append(
            Coordinate(
                raw=match.group(0),
                system=CoordSystem.PLACEHOLDER if placeholder else CoordSystem.VN_GRID,
                zone=None,
                vn_ref=match.group(0).replace(" ", ""),
                datum=datum.group("datum") if datum else None,
                accuracy_code=accuracy.group("code").upper() if accuracy else None,
                altitude_m=float(re.sub(r"[^\d.]", "", altitude.group("value")))
                if altitude
                else None,
            )
        )

    if not found and (match := COORD_PLACEHOLDER_RE.search(text)):
        recorder.add(
            AnomalyCode.COORD_PLACEHOLDER,
            f"unrecorded coordinate {match.group(0).strip()!r}",
            field_path="header.coordinates",
        )
        found.append(
            Coordinate(
                raw=match.group(0).strip(),
                system=CoordSystem.PLACEHOLDER,
                datum=datum.group("datum") if datum else None,
                altitude_m=float(re.sub(r"[^\d.]", "", altitude.group("value")))
                if altitude
                else None,
            )
        )
    return found


def _add_latlon(coordinate: Coordinate, recorder: AnomalyRecorder) -> None:
    """Derive WGS84 lat/lon, refusing to convert an implausible coordinate.

    ETRS89 and WGS84 differ by well under a metre in Cantabria, so the datum
    distinction is not worth a PROJ dependency here.
    """
    if coordinate.easting is None or coordinate.northing is None:
        return
    if not (
        EASTING_RANGE[0] <= coordinate.easting <= EASTING_RANGE[1]
        and NORTHING_RANGE[0] <= coordinate.northing <= NORTHING_RANGE[1]
    ):
        recorder.add(
            AnomalyCode.COORD_OUT_OF_RANGE,
            f"{coordinate.raw!r} is outside the Matienzo depression",
            field_path="header.coordinates",
        )
        return

    zone_number = int(coordinate.zone[:2]) if coordinate.zone else 30
    zone_letter = coordinate.zone[2] if coordinate.zone and len(coordinate.zone) > 2 else "T"
    latitude, longitude = utm.to_latlon(
        coordinate.easting, coordinate.northing, zone_number, zone_letter
    )
    coordinate.latitude = round(latitude, 7)
    coordinate.longitude = round(longitude, 7)


def _parse_quantities(
    raw_html: str, recorder: AnomalyRecorder, confidence: ConfidenceScorer
) -> tuple[list[Quantity], list[str], list[str]]:
    """Scan `<B>Label</B> value` pairs across the whole info line.

    Values run from the end of one label to the start of the next, which is what
    lets a prose Length ("included in the length of 0246") be captured whole
    instead of truncated at the first tag.
    """
    labels = list(LABEL_RE.finditer(raw_html))
    quantities: list[Quantity] = []
    order: list[str] = []
    seen: set[str] = set()
    duplicates: list[str] = []

    for index, match in enumerate(labels):
        label_raw = unescape(match.group("label")).strip()
        canonical = MEASUREMENT_LABELS.get(re.sub(r"\s+", " ", label_raw.lower()).rstrip(":"))
        if canonical is None:
            continue
        if canonical == "altitude":
            continue  # altitude is an entrance property; see `_parse_coordinates`

        end = labels[index + 1].start() if index + 1 < len(labels) else len(raw_html)
        value_html = raw_html[match.end() : end]
        quantity = _parse_quantity(canonical, label_raw, value_html, recorder)
        quantities.append(quantity)

        order.append(canonical)
        if canonical in seen:
            duplicates.append(canonical)
        seen.add(canonical)

        if quantity.kind is QuantityKind.UNKNOWN:
            confidence.deduct("header", 0.05, f"{canonical} value unparsed")

    return quantities, order, duplicates


def _parse_quantity(
    label: str, label_raw: str, value_html: str, recorder: AnomalyRecorder
) -> Quantity:
    """Interpret one measurement value.

    The order of attempts matters: a compound `5 & 5m` and a range `from 232m …
    to 130m` both contain something that looks like a plain number, so they are
    tested first.
    """
    raw = strip_tags(value_html)
    quantity = Quantity(label=label, label_raw=label_raw, raw=raw, kind=QuantityKind.UNKNOWN)

    if not raw:
        return quantity

    if UNKNOWN_VALUE_RE.match(raw):
        # `?m` — the field was filled in to say the value is not known. That is
        # a different fact from the label being absent, so it is recorded.
        quantity.modifier = Modifier.UNCERTAIN
        return quantity

    if match := COMPOUND_RE.match(raw):
        parts = [float(match.group("first")), float(match.group("second"))]
        quantity.kind = QuantityKind.COMPOUND
        quantity.parts = parts
        quantity.value_m = sum(parts)
        if match.group("query"):
            quantity.modifier = Modifier.UNCERTAIN
        return quantity

    if match := RANGE_RE.search(raw):
        quantity.kind = QuantityKind.RANGE
        quantity.min_m = float(match.group("low"))
        quantity.max_m = float(match.group("high"))
        quantity.value_m = quantity.max_m - quantity.min_m
        return quantity

    if (match := VALUE_RE.match(raw)) and match.group("number"):
        value = float(match.group("number"))
        quantity.kind = QuantityKind.NUMERIC
        quantity.unit_stated = bool(match.group("unit"))
        if match.group("sign"):
            quantity.value_m = -value if match.group("sign") == "-" else value
            quantity.modifier = Modifier.RELATIVE if label == "vertical_range" else Modifier.EXACT
        else:
            quantity.value_m = value
        if match.group("atmost"):
            quantity.modifier = Modifier.AT_MOST
        elif match.group("circa"):
            quantity.modifier = Modifier.CIRCA
        elif match.group("plus"):
            quantity.modifier = Modifier.AT_LEAST
        elif match.group("query"):
            quantity.modifier = Modifier.UNCERTAIN
        # Trailing prose after a number, e.g. `44m to downstream sump`, is kept
        # in `raw` and needs no separate handling.
        return quantity

    if re.search(r"[A-Za-z]{3}", raw):
        quantity.kind = QuantityKind.PROSE
        quantity.prose = _parse_prose(raw, value_html)
        recorder.add(
            AnomalyCode.MEASURE_PROSE,
            f"{label} is prose: {raw[:80]!r}",
            field_path=f"header.quantities.{label}",
        )
        return quantity

    recorder.add(
        AnomalyCode.MEASURE_UNPARSED,
        f"cannot interpret {label} value {raw[:60]!r}",
        field_path=f"header.quantities.{label}",
    )
    return quantity


def _parse_prose(raw: str, value_html: str) -> ProseQuantity:
    """Extract the relation, targets and system name from a prose measurement.

    This is the payoff for not modelling Length as `float | None`: the result
    becomes a system-membership row and a cross-reference edge, and the original
    sentence is still there in `raw`.
    """
    prose = ProseQuantity()
    for pattern, relation in PROSE_PATTERNS:
        if pattern.search(raw):
            prose.relation = relation
            break

    prose.target_sites = sorted(
        {int(n) for n in SITE_LINK_RE.findall(value_html)}
        | {int(n) for n in SITE_TEXT_RE.findall(raw)}
    )

    if match := SYSTEM_NAME_RE.search(raw):
        name = " ".join(match.group(0).split())
        if len(name) > 4 and not name.lower().startswith(("see ", "the ")):
            prose.system_name = name

    # `(870m added to Risco)` — a length that belongs to a different site.
    if prose.relation is ProseRelation.ADDED_TO and (match := EMBEDDED_M_RE.search(raw)):
        prose.extra_value_m = float(match.group(1))

    names = re.findall(r"\b(?:see|in|into)\s+((?:[A-Z][\w'’-]*\s*){1,4})", raw)
    prose.target_names = [" ".join(n.split()) for n in names if len(n.strip()) > 2]
    return prose
