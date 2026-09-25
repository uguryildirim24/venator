"""Every text file this package opens names its encoding, and its stores name LF.

Two properties, one about decoding and one about newline bytes, both invisible
on this suite's own platform and both live bugs on another one.

**Encoding.** Python on Windows defaults a text-mode file to the ANSI code page
— cp1252 on a stock US/Western-Europe install — not UTF-8, and will until
PEP 686's UTF-8 mode becomes the default in a later release than this project
pins. That is not a hypothetical mismatch: this repository's own committed
corpus contains bytes cp1252 has no mapping for, and profiles/example/resume.yaml
does too, so Discover, Tailor and the resume CLI died on the Owner's own data.
On Linux and macOS the locale is already UTF-8 and an omitted `encoding=` is
invisible, which is exactly why a guard is worth more here than a fixed bug.

**Newline.** A text-mode write on Windows translates every ``\\n`` to ``\\r\\n``.
`data/` is append-only (ADR-0001) — a line goes in and is never rewritten — so a
CRLF terminator written into a day file is permanent, and no `.gitattributes`
can take it back out because the bytes were already CRLF before Git saw them.
The writers therefore pin the terminator themselves — **every** writer in this
package, not only the ones that append. Asking only about append mode is how the
Profile claim stamp and the application bundle's `meta.json` got missed: both are
`write_text`, which has no mode at all, and both go to paths `.gitattributes`
pins to LF. A rule that has to be told which files matter will keep missing the
next one, so this one is not told.

This walks the AST rather than grepping, because a grep for `encoding=` cannot
tell an argument of *this* call from one on the next line, cannot see a mode
given positionally, and cannot tell a binary open from a text one.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "venator"

# The three ways this package opens a text file. `open` is the builtin;
# the other two are pathlib's, and both are text-mode by definition — there is
# no binary `read_text`.
OPENERS = ("open", "fdopen", "read_text", "write_text")


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _mode(node: ast.Call) -> str:
    """The mode this call asks for, positional or keyword, "" when it says none.

    Only `open` has one. `read_text`/`write_text` are text mode by definition,
    and `write_text`'s first positional argument is the payload — reading that
    as a mode would call every write binary and skip the whole class.
    """
    if _called_name(node) not in {"open", "fdopen"}:
        return ""
    given = _keyword(node, "mode")
    if given is None and node.args:
        # The builtin is `open(path, mode)`; pathlib's is `path.open(mode)`,
        # where the receiver sits on `func` rather than in `args`.
        first_mode_argument = 1 if isinstance(node.func, ast.Name) or _called_name(node) == "fdopen" else 0
        if len(node.args) > first_mode_argument:
            given = node.args[first_mode_argument]
    if isinstance(given, ast.Constant) and isinstance(given.value, str):
        return given.value
    return ""


def _calls() -> list[tuple[Path, ast.Call, str]]:
    found: list[tuple[Path, ast.Call, str]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name not in OPENERS:
                continue
            # os.open creates a raw file descriptor with integer flags. Its
            # text wrapper (os.fdopen) is where encoding is required.
            if (isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os" and name == "open"):
                continue
            found.append((path, node, _mode(node)))
    return found


def _where(path: Path, node: ast.Call) -> str:
    return f"{path.relative_to(SOURCE_ROOT.parents[1])}:{node.lineno}"


def test_every_text_mode_open_names_its_encoding() -> None:
    missing = [
        _where(path, node)
        for path, node, mode in _calls()
        if "b" not in mode and _keyword(node, "encoding") is None
    ]

    assert missing == [], (
        "these open a text file without saying which encoding, so on Windows "
        "they use the ANSI code page and cannot read this repository's own "
        f"committed corpus: {missing}"
    )


def _writes(name: str, mode: str) -> bool:
    """Does this call open a text file for writing?

    `write_text` always does. `open` does when its mode asks to write, append or
    update. A read does not need the terminator pinned — `newline=None` on the
    way *in* is universal-newline translation, which is what a reader wants.
    """
    if name == "write_text":
        return True
    return any(character in mode for character in "wax+")


def test_every_text_mode_write_pins_its_line_terminator() -> None:
    # Deliberately every write, not only every append. An earlier version of
    # this asked only about `"a" in mode`, and a `write_text` has no mode at
    # all — so the Profile claim stamp (`data/decisions/.profile`) and the
    # application bundle's `meta.json` were both structurally invisible to it,
    # and both wrote CRLF on Windows into paths .gitattributes pins to LF. The
    # rule is now mechanical rather than a judgement about which files matter:
    # no writer in this package leaves the terminator to the platform, so
    # nothing has to be remembered when the next one is added.
    unpinned = [
        _where(path, node)
        for path, node, mode in _calls()
        if _writes(_called_name(node) or "", mode)
        and "b" not in mode
        and _keyword(node, "newline") is None
    ]

    assert unpinned == [], (
        "these write a text file without pinning the line terminator, so on "
        f"Windows they would write CRLF: {unpinned}"
    )
