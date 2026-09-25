"""Resolve one pasted URL to board tokens, in JSON, for the onboarding surface.

    python resolve_board.py https://www.example.com/careers

This is an **adapter**, not a second resolver. Every rule it applies — what a
pasted URL may look like, which ATS board URLs are recognised, how a Workday
site string is learned from ``robots.txt``, and whether a board answers — comes
from ``venator.discover.register``, which is the pipeline's own module and stays
the only place those rules live. A second copy of them in TypeScript is exactly
the drift that put twenty-three unregistered board tokens on screen once
already; this file exists so that never has to happen again.

It is the mirror of what ``server/onboarding/runtime.ts`` does with
``venator.llm.probe``: one subprocess, one JSON document, one normalisation on
the Node side.

Three properties are deliberate:

* **Read-only HTTP, and nothing else.** ``register`` issues ``GET`` requests and
  follows no redirect. Nothing here posts, and nothing here is an application to
  an employer — the never-submit invariant is untouched.
* **No employer name is invented.** ``register`` refuses to, and so does this:
  the page ``<title>`` travels as a *hint* the Owner reads while typing the real
  name, and never as the name.
* **A caught exception's text never reaches the browser.** ``register`` raises
  ``RegisterError`` with wording written for a person, and that wording is
  passed along. Everything else is reported as a category with a message written
  here, and the original goes to stderr where the server logs it.

Exit codes: ``0`` a JSON document on stdout, ``3`` the pipeline module could not
be imported, ``2`` this script was called wrongly.
"""

from __future__ import annotations

import contextlib
import json
import sys

# `register` prints progress to stdout as it works. That would land in the
# middle of this document, so stdout is held aside and every print goes to
# stderr; the JSON is written to the real stdout at the very end.
_STDOUT = sys.stdout

try:
    import httpx

    from venator.discover.register import (
        Confirmation,
        RegisterError,
        confirm,
        resolve,
    )
except Exception as err:  # pragma: no cover - exercised by an Install without the pipeline
    print(f"resolve_board: the pipeline could not be imported: {err}", file=sys.stderr)
    raise SystemExit(3) from err


def _document(url: str, title: str | None, results: list[Confirmation]) -> dict:
    return {
        "url": url,
        "titleHint": title,
        "boards": [
            {
                "source": result.board.source,
                "board": result.board.board,
                "route": result.board.route,
                "confirmed": result.confirmed,
                "postingCount": result.posting_count,
                "complete": result.complete,
            }
            for result in results
        ],
    }


def _failure(kind: str, message: str | None) -> dict:
    return {"failure": {"kind": kind, "message": message}}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("resolve_board: expected exactly one URL", file=sys.stderr)
        return 2
    url = argv[0]
    with contextlib.redirect_stdout(sys.stderr):
        try:
            boards, title = resolve(url)
            results = confirm(boards)
        except RegisterError as err:
            # `register` writes these for the person reading them, and they name
            # only the URL that was pasted. Passed through unchanged.
            payload = _failure("unresolvable", str(err))
        except httpx.HTTPError as err:
            print(f"resolve_board: {type(err).__name__}: {err}", file=sys.stderr)
            payload = _failure(
                "unreachable",
                "That page could not be reached from this machine, so nothing could be read off it.",
            )
        except Exception as err:  # pragma: no cover - the unclassified case
            print(f"resolve_board: {type(err).__name__}: {err}", file=sys.stderr)
            payload = _failure("failed", None)
        else:
            payload = _document(url, title, results)
    json.dump(payload, _STDOUT)
    _STDOUT.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
