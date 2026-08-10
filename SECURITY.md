# Security policy

## What this project is

`matienzo-caves` is local-first tooling: a pipeline that turns a committed
corpus of HTML pages into a SQLite database, a CLI that queries it, and an MCP
server that exposes that database to a local model client. There is no hosted
service, no user accounts, no network listener, and no data belonging to anyone
but the Matienzo Caves Project's published cave descriptions. Read a report in
that light — the realistic harm is to a person running this code on their own
machine, not to a deployment.

## Supported versions

Only the tip of `main`. There are no releases, no tags, and no backports; a fix
lands on `main` and that is the supported version. If you are running a clone,
pull before reporting.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting — the **Report a vulnerability**
button under the repository's [Security tab][advisories]. It is enabled, and it
keeps the report private until there is a fix.

If you would rather use email, write to <andrew@northall.me.uk>.

Please do not open a public issue for something exploitable. Public issues are
the right place for everything else, including hardening ideas that do not
depend on a working exploit.

A useful report says which entry point is affected (`matienzo` CLI subcommand,
`matienzo-mcp`, or a script under `scripts/`), what an attacker has to control
to reach it, and what they get. A reproduction against a fresh clone is worth
more than a description.

This is a spare-time project maintained by one person. I aim to acknowledge a
report within seven days and to say by then whether I agree it is a
vulnerability and roughly when I can fix it. If you have had no reply in two
weeks, assume the mail went astray and chase it on the advisory thread.

Please give a fix a reasonable window before publishing — 90 days is the usual
figure and is more than enough here. Credit goes in the advisory unless you ask
otherwise.

[advisories]: https://github.com/anorthall/matienzo-caves/security/advisories

## Trust boundaries

These are the places where this code handles something it did not write, and so
the places a real vulnerability is most likely to be. Reports against them are
in scope.

**The corpus, and re-fetched pages.** `data/pages/` is committed and parsed with
lxml. `scripts/download_htm.py` re-fetches from the live site, which is
hand-edited and can change under you. A page that makes the parser hang, consume
unbounded memory, or read or write outside the repository is a bug worth
reporting — as is anything that gets lxml to resolve an external entity or fetch
a network resource while parsing.

**The MCP `sql` tool.** `matienzo-mcp` exposes an arbitrary query string to
whatever model client is driving it. Two things are supposed to contain it: the
connection is opened `mode=ro` so SQLite itself refuses writes, and a regex pair
in `matienzo/mcp_server.py` restricts the input to a single `SELECT` or `WITH`.
The regex is a convenience, not the security boundary — but a query that writes
to the database, reads a file outside it, loads an extension, or otherwise
escapes read-only mode is a genuine finding. So is anything that turns corpus
text returned by a tool into instructions the calling model acts on.

**Loadable extensions and downloaded models.** The database connection enables
extension loading so `sqlite-vec` can be loaded, and the `embed` extra downloads
`BAAI/bge-small-en-v1.5` (~130 MB of ONNX) from Hugging Face on first use. If
either can be pointed at something the user did not intend — a path, a mirror, a
model file — that is in scope.

**The dependency chain.** Dependencies are pinned in `uv.lock`, CI installs with
`uv sync --frozen`, and Dependabot security updates are on. Report a known-
vulnerable pin that is reachable from this code; it will be treated as a normal
bug rather than an embargoed advisory unless there is an exploit path here.

## Out of scope

- The content of `matienzocaves.org.uk` itself, or its hosting. This repository
  only reads published pages; report site issues to the Matienzo Caves Project.
- Findings that require an attacker who already has code execution or write
  access on the machine running the pipeline. The database is a disposable
  artefact rebuilt from `data/`; someone who can edit `data/` can already change
  the output, and that is the design rather than a flaw.
- Reports that a build consumes a lot of CPU, memory, or disk when pointed at a
  large corpus by the person running it.
- Automated scanner output with no demonstrated path through this code.
- Anything about a deployment of this project that this repository does not
  describe. There is no server here to attack.

## Secrets

There are none, and none should ever be committed: nothing in this project
authenticates to anything. If you find a credential in the history, report it
privately as above and I will treat it as compromised regardless of what it
looks like.
