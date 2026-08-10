# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28"]
# ///
"""Check a deployed portal, against the real URL.

Everything here is a property that a `TestClient` run cannot observe, because
every one of them is about what sits between the app and the visitor.

The load-bearing check is time-to-first-delta. If the reverse proxy buffers the
response — nginx does by default — the whole answer still arrives, correct and
complete, at the end. Nothing errors and nothing logs. The only symptom is that
the first byte turns up when the last one does, which is exactly what this
measures.

Run it against a deployment, not against localhost:

    uv run deploy/smoke.py https://caves.example.org
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

#: A first token later than this means the stream is being buffered somewhere,
#: even if the answer itself is fine.
FIRST_DELTA_BUDGET_SECONDS = 5.0

#: How long to wait for the whole answer before giving up on it.
TOTAL_BUDGET_SECONDS = 120.0

QUESTION = "Which caves are in Cobadal?"


class Failed(Exception):
    pass


def check_health(client: httpx.Client) -> dict[str, object]:
    response = client.get("/healthz", timeout=15)
    response.raise_for_status()
    body: dict[str, object] = response.json()

    if not body.get("corpus"):
        raise Failed("the corpus did not open read-only — is the database still in WAL mode?")
    if not body.get("embeddings"):
        raise Failed("the embedding model did not load; search is keyword-only")
    print(f"  health   ok · mode={body.get('mode')} · model={body.get('model')}")
    return body


def check_stream(client: httpx.Client) -> None:
    started = time.monotonic()
    first_delta: float | None = None
    seen: list[str] = []
    sources = 0

    with client.stream(
        "POST",
        "/api/chat",
        json={"question": QUESTION},
        timeout=httpx.Timeout(TOTAL_BUDGET_SECONDS, read=TOTAL_BUDGET_SECONDS),
    ) as response:
        if response.status_code == 429:
            raise Failed("rate limited before the smoke test could run")
        response.raise_for_status()

        if response.headers.get("content-type", "").split(";")[0] != "text/event-stream":
            raise Failed(f"expected an event stream, got {response.headers.get('content-type')!r}")

        name: str | None = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: ") and name is not None:
                payload = json.loads(line.removeprefix("data: "))
                seen.append(name)
                if name == "delta" and first_delta is None:
                    first_delta = time.monotonic() - started
                if name == "source":
                    sources += 1
                if name == "error":
                    raise Failed(f"the portal reported {payload.get('code')}: {payload}")
                name = None

    total = time.monotonic() - started

    if "start" not in seen:
        raise Failed("no start event")
    if seen[-1] != "done":
        raise Failed(f"the stream ended on {seen[-1]!r} rather than done")
    if first_delta is None:
        raise Failed("no answer text arrived")
    if sources == 0:
        raise Failed("no sources were cited")

    print(f"  sources  {sources}")
    print(f"  answer   first token {first_delta:.2f}s · complete {total:.2f}s")

    if first_delta > FIRST_DELTA_BUDGET_SECONDS:
        raise Failed(
            f"first token took {first_delta:.1f}s (budget {FIRST_DELTA_BUDGET_SECONDS}s).\n"
            "    The answer arrived, so the app is fine — something between it and\n"
            "    here is buffering the response. Check `proxy_buffering off` in the\n"
            "    /api/chat location (deploy/nginx.conf)."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="e.g. https://caves.example.org")
    args = parser.parse_args()

    print(f"Smoke-testing {args.base_url}")
    with httpx.Client(base_url=args.base_url, follow_redirects=True) as client:
        try:
            check_health(client)
            check_stream(client)
        except (Failed, httpx.HTTPError) as error:
            print(f"\nFAILED: {error}", file=sys.stderr)
            return 1

    print("\nAll good.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
