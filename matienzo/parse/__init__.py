"""Pure parsing. No database, no network, no filesystem beyond reading a page.

`parse_document(bytes) -> ParsedSite` must stay a pure function of its input plus
`PARSER_VERSION` and the vocab files. That purity is what makes golden-file tests
meaningful and what lets `matienzo build` be idempotent.
"""
