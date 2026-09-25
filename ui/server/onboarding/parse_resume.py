"""Read an uploaded resume PDF, in JSON, for the onboarding surface.

    python parse_resume.py --mode text  < resume.pdf
    python parse_resume.py --mode model [--lane NAME] < resume.pdf

This is an **adapter**, not a second parser. Every rule it applies — what a PDF
may be, how long it may be, what a model may be asked, and above all which of the
model's answers are grounded in the document and which are discarded — comes from
``venator.resume.parse``, which is the pipeline's own module and stays the only
place those rules live. It is the mirror of what ``resolve_board.py`` does for
``venator.discover.register`` and ``verify_profile.py`` does for
``venator.profile``: one subprocess, one JSON document on stdout, one
normalisation on the Node side.

Four properties are deliberate:

* **It writes nothing and starts nothing.** No Profile is created or edited, no
  store is stamped, no Posting's lifecycle state is touched, and no request is
  made of any employer. The bytes arrive on stdin and one document leaves on
  stdout. What the pipeline eventually writes, the ``profile`` route writes,
  after a person has confirmed every field on screen and after the load-back.
* **``--mode text`` spends nothing.** It extracts and returns the text, and
  onboarding's own deterministic reading (``server/onboarding/resume.ts``) runs
  over it. That is the free route, and it is the whole route for an Install with
  no assistant connected. Those regular expressions are not duplicated here:
  ``src/venator/`` never imports from ``ui/``, and a second copy of them in a
  second language is exactly the drift this repository has already paid for
  twice.
* **``--mode model`` spends one completion**, through ``venator.llm.complete``
  and nothing else. There is no API client here, no key, no second spawn and no
  ladder between lanes. ``--lane NAME`` sets ``VENATOR_LLM_RUNTIME`` in *this*
  process before the pipeline is asked, which is the only supported way to name
  a lane; an unrecognised name is a ``failed`` outcome naming the stage and never
  an echo of what was passed.
* **Nothing it emits is confirmed.** Every section of a proposal carries
  ``"confirmed": false``, because the screen has to mark it unconfirmed and a
  screen cannot be relied on to remember. Nothing here has been seen by the
  person it is about.

The document on stdout is exactly one of:

    {"outcome":"read","mode":"text","text":S,"pages":N,"characters":N,
     "truncated":bool}
    {"outcome":"parsed","mode":"model","resume":{...},"sections":[...],
     "pages":N,"characters":N,"truncated":bool,"dropped":N,
     "schemaEnforced":bool}
    {"outcome":"unreadable","reason":"not_a_pdf"|"encrypted"|"no_text"|
     "too_large"|"too_many_pages"|"malformed"}
    {"outcome":"no_runtime"}
    {"outcome":"failed","stage":"extract"|"complete"|"decode"}

``no_runtime`` carries no detail on purpose: the runtime step of onboarding
already tells a person what is missing and how to fix it, and a detail field here
is a place for a runtime's own text to end up on a screen. ``failed`` carries a
stage name and nothing else, for the same reason.

Exit codes: ``0`` a JSON document on stdout, ``3`` the pipeline module could not
be imported, ``2`` this script was called wrongly.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys

# `pypdf` warns through `logging`, and a stage that starts printing to stdout
# would land in the middle of this document. stdout is held aside and every
# print goes to stderr; the JSON is written to the real stdout at the very end.
# Exactly as the other three adapters do it.
_STDOUT = sys.stdout

#: The variable `venator.llm.adapter` reads to choose a lane. Named here rather
#: than passed as an argument to `complete`, because `complete` takes no lane
#: parameter and adding one would be a second selection rule beside the
#: documented one.
RUNTIME_VARIABLE = "VENATOR_LLM_RUNTIME"

try:
    from venator.llm import NoRuntimeAvailable
    from venator.resume.parse import (
        MAXIMUM_PDF_BYTES,
        ExtractedDocument,
        ParsedResume,
        ResumeParseFailed,
        ResumeUnreadable,
        extract_text,
        parse_with_model,
    )
except Exception as err:  # pragma: no cover - exercised by an Install without the pipeline
    print(f"parse_resume: the pipeline could not be imported: {err}", file=sys.stderr)
    raise SystemExit(3) from err


def _read(document: ExtractedDocument) -> dict:
    return {
        "outcome": "read",
        "mode": "text",
        "text": document.text,
        "pages": document.pages,
        "characters": document.characters,
        "truncated": document.truncated,
    }


def _parsed(document: ExtractedDocument, parsed: ParsedResume) -> dict:
    return {
        "outcome": "parsed",
        "mode": "model",
        "resume": dict(parsed.resume),
        "sections": [
            {
                "section": report.section,
                "entries": report.entries,
                "fields": report.fields,
                "filled": report.filled,
                "dropped": report.dropped,
                "confirmed": report.confirmed,
            }
            for report in parsed.sections
        ],
        "pages": document.pages,
        "characters": document.characters,
        "truncated": document.truncated,
        "dropped": parsed.dropped,
        "schemaEnforced": parsed.schema_enforced,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read an uploaded resume PDF.")
    parser.add_argument("--mode", required=True, choices=("text", "model"))
    parser.add_argument("--lane", default=None)
    args = parser.parse_args(argv)

    # One byte past the bound, never the whole stream: a stdin nobody bounded is
    # a memory question this process should not have, and one extra byte is all
    # `extract_text` needs to refuse it as `too_large`. The Node side bounds the
    # upload as well; this holds whether or not it does.
    pdf = sys.stdin.buffer.read(MAXIMUM_PDF_BYTES + 1)

    with contextlib.redirect_stdout(sys.stderr):
        payload: dict
        try:
            document = extract_text(pdf)
        except ResumeUnreadable as err:
            payload = {"outcome": "unreadable", "reason": err.reason}
        except Exception as err:  # pragma: no cover - the unclassified case
            print(f"parse_resume: {type(err).__name__}: {err}", file=sys.stderr)
            payload = {"outcome": "failed", "stage": "extract"}
        else:
            if args.mode == "text":
                payload = _read(document)
            else:
                # Set in this process only, and only when a lane was named. An
                # unrecognised name reaches `complete`, which raises ValueError
                # naming the lanes that do exist — that sentence goes to stderr
                # for the server's log and comes back as a stage, never as an
                # echo of what was passed.
                if args.lane is not None:
                    os.environ[RUNTIME_VARIABLE] = args.lane
                try:
                    payload = _parsed(document, parse_with_model(document))
                except NoRuntimeAvailable as err:
                    print(f"parse_resume: {err}", file=sys.stderr)
                    payload = {"outcome": "no_runtime"}
                except ResumeParseFailed as err:
                    print(f"parse_resume: {err}", file=sys.stderr)
                    payload = {"outcome": "failed", "stage": err.stage}
                except Exception as err:  # pragma: no cover - the unclassified case
                    print(f"parse_resume: {type(err).__name__}: {err}", file=sys.stderr)
                    payload = {"outcome": "failed", "stage": "complete"}

    json.dump(payload, _STDOUT)
    _STDOUT.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
