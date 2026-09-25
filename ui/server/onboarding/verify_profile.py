"""Load a candidate Profile directory, in JSON, for the onboarding write surface.

    python verify_profile.py --directory /path/to/a/staged/profile

This is an **adapter**, not a second loader. The question it answers is the only
question that matters about a Profile onboarding is about to move into place:
*does ``src/venator/profile/`` read it back?* That question has exactly one
correct answer and ``venator.profile.load_profile`` is the only thing that
knows it — PyYAML is YAML 1.1, its reader refuses codepoints no JSON escaper
escapes, and a Profile that half-loads is worse than one that does not load at
all, because a ``targeting.yaml`` with no Hard Filter in it passes every Posting.

It is the mirror of what ``server/onboarding/resolve_board.py`` does for
``venator.discover.register`` and ``server/runs/plan.py`` does for the run
surface: one subprocess, one JSON document on stdout, one normalisation on the
Node side.

**Why a subprocess and not a check in TypeScript.** A second YAML reader in a
second language would be a second opinion, and the whole defect this file was
written for is what happens when the two opinions differ: the emitter believed
``JSON.stringify`` produced a scalar YAML reads back character for character, and
for U+0085 — which PyYAML treats as a line break — it does not. The emitter is
fixed. This exists so that the next thing the emitter's author did not think of
is caught by the loader itself rather than by a person whose Profile stopped
loading.

Three properties are deliberate:

* **It only reads.** No file is created, moved or written, no store is stamped,
  no stage is run and no request leaves this machine. It is handed a directory
  that is already fully written and answers whether the pipeline can read it.
* **A refusal is data.** A Profile the loader rejects comes back as an
  ``outcome`` of ``refused`` — exit 0 — so the caller can decline to move it into
  place rather than rendering a crash.
* **The refusal's text is for the server's log, never for a screen.** The
  directory being checked is a staging directory inside somebody's application
  data directory, and ``ProfileError`` names it. The Node side logs this message
  and answers the browser with a sentence of its own.

Exit codes: ``0`` a JSON document on stdout, ``3`` the pipeline module could not
be imported, ``2`` this script was called wrongly.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

# The loader announces nothing today, but a stage that starts printing to stdout
# would land in the middle of this document. stdout is held aside and handed
# back only for the document, exactly as the other two adapters do it.
_STDOUT = sys.stdout

try:
    from venator.profile import ProfileError, load_profile
except Exception as err:  # pragma: no cover - exercised by an Install without the pipeline
    print(f"verify_profile: the pipeline could not be imported: {err}", file=sys.stderr)
    raise SystemExit(3) from err


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Does the pipeline read this Profile back?")
    parser.add_argument("--directory", required=True)
    args = parser.parse_args(argv)

    with contextlib.redirect_stdout(sys.stderr):
        try:
            load_profile(Path(args.directory))
        except ProfileError as err:
            # Every YAML failure arrives here too: `_read_yaml` catches
            # `yaml.YAMLError` — which is the reader's "unacceptable character",
            # the scanner's refusals and every parse failure — and re-raises it
            # as a `ProfileError` naming the file.
            payload = {"outcome": "refused", "message": str(err)}
        except Exception as err:  # pragma: no cover - the unclassified case
            print(f"verify_profile: {type(err).__name__}: {err}", file=sys.stderr)
            payload = {"outcome": "failed"}
        else:
            payload = {"outcome": "loads"}

    json.dump(payload, _STDOUT)
    _STDOUT.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
