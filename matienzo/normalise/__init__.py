"""Map what a page says onto canonical entities.

The division of labour with `parse/` is deliberate and one-way: parsers record
the page's own words, normalisers resolve them against `data/vocab/`. Both
values survive into the database — `site.area_raw` beside `site.area_id` — so a
normalisation mistake is always recoverable and never silently rewrites the
source. A normaliser must never destroy the raw string.
"""
