"""The Posting store reads and writes its own corpus where UTF-8 is not the default.

Windows decides a text file's encoding from the ANSI code page unless the call
says otherwise, and on a stock US/Western-Europe install that is cp1252. This
fixture makes that the default here, so a missing `encoding=` fails on Linux
instead of only on the machine nobody in CI has.

Both halves of the store break without it, and both break on ordinary data
rather than exotic data:

* reading — `data/postings/2026-08-18.jsonl` carries bytes cp1252 has no mapping
  for, so loading Postings raises before Discover has fetched anything at all;
* writing — Postings are serialised with `ensure_ascii=False`, and employer
  descriptions routinely carry en dashes, curly quotes and non-breaking hyphens,
  none of which cp1252 can encode. The append is line-buffered, so a run that
  dies mid-batch leaves a partial day file in an append-only store, permanently.

The third assertion is about newline bytes rather than encoding: a text-mode
write on Windows turns every ``\\n`` into ``\\r\\n``, and a CRLF terminator in a
day file can never be taken back out (ADR-0001).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.discover.store import load_postings, refresh_postings

# A non-breaking hyphen and a right double quotation mark: both live in this
# repository's committed corpus, and neither exists in cp1252.
UNMAPPABLE = "Senior Scientist‑II “bench”"


@pytest.fixture
def ansi_code_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make an unspecified encoding mean cp1252 and an unspecified newline CRLF.

    Both are what Windows does to a text file the caller did not pin, and both
    are invisible here otherwise. Only `Path.open` is patched, because it is the
    one door: `read_text` and `write_text` both go through it. They arrive with
    `encoding="locale"` rather than `None` — `io.text_encoding` substitutes that
    for an omitted argument — so both spellings of "whatever this machine says"
    are caught. On the write side an unpinned newline is `os.linesep`, which
    `newline="\r\n"` reproduces exactly; on the read side an unpinned newline
    means universal newlines on every platform, so it is left alone.
    """
    real_open = Path.open

    def like_windows(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> object:
        if "b" not in mode:
            if encoding in (None, "locale"):
                encoding = "cp1252"
            if newline is None and ("w" in mode or "a" in mode or "x" in mode):
                newline = "\r\n"
        return real_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", like_windows)


def posting(key: str, title: str) -> dict[str, str]:
    return {
        "key": key,
        "source": "greenhouse",
        "board": "x",
        "title": title,
        "company": "Ginkgo Bioworks",
    }


def test_a_stored_posting_is_read_back_wherever_the_code_page_is_not_utf8(
    tmp_path: Path, ansi_code_page: None
) -> None:
    store = tmp_path / "postings"
    store.mkdir()
    (store / "2026-08-18.jsonl").write_bytes(
        (json.dumps(posting("greenhouse:x:1", UNMAPPABLE), ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
    )

    assert {item["key"] for item in load_postings(store)} == {"greenhouse:x:1"}


def test_a_posting_with_an_ordinary_dash_can_still_be_appended(
    tmp_path: Path, ansi_code_page: None
) -> None:
    store = tmp_path / "postings"

    assert refresh_postings(
        store, [posting("greenhouse:x:2", UNMAPPABLE)], complete=False, status="partial"
    ).appended == 1

    written = next(store.glob("*.jsonl"))
    assert json.loads(written.read_text(encoding="utf-8"))["title"] == UNMAPPABLE


def test_an_appended_line_ends_with_one_byte_and_it_is_lf(
    tmp_path: Path, ansi_code_page: None
) -> None:
    store = tmp_path / "postings"
    refresh_postings(
        store, [posting("greenhouse:x:3", "Scientist")], complete=False, status="partial"
    )

    raw = next(store.glob("*.jsonl")).read_bytes()

    assert b"\r" not in raw
    assert raw.endswith(b"}\n")
