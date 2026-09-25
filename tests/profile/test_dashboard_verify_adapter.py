"""What the dashboard is allowed to write, asked of the loader that reads it.

``ui/server/onboarding/verify_profile.py`` is the onboarding surface's load-back
adapter. Two routes — ``POST /api/onboarding/profile`` and
``POST /api/onboarding/employers`` — stage what they are about to write, run this
against the staging directory, and perform the rename **only** if it answers that
the Profile loads. A refusal leaves the file that was on disk byte-identical and
leaves an absent one uncreated.

This file is the Python half of three claims the TypeScript side cannot make:

* **The adapter answers as its contract says**, for a Profile that loads and for
  one that does not, and it does it without writing anything.
* **The emitter's escape rules are the loader's.** ``server/onboarding/yaml.ts``
  carries two codepoint sets — the ones PyYAML cannot read out of a file
  literally, and the plain words YAML 1.1 resolves to something that is not text.
  Both are measurements of PyYAML, and a measurement written down in another
  language is a claim nobody re-checks. These cases are the check: they go red if
  PyYAML's behaviour ever moves, which is the moment that constant is wrong.
* **What the route writes, the Match stage can use.** "The loader reads it back"
  and "the pipeline can run on it" are two different claims, and the second one
  is only sayable here: ``filters_version()`` is
  ``src/venator/match/store.py``'s, and it re-serializes exactly the file the
  employer route writes.

**These cases run the shipping emitter; they do not re-implement it.** They used
to. ``_emitted`` was a Python copy of ``quoteScalar`` over a hardcoded list of
codepoints, so the sweep below certified a second implementation and nothing tied
it to ``yaml.ts`` at all — narrowing that file's escape range reintroduced the
original defect for ten codepoints while every suite stayed green. The emitter is
now spawned, over the same one-process-one-JSON-document seam the dashboard uses
everywhere else: ``ui/tests/quote-scalars.ts`` for a scalar and
``ui/tests/register-employers.ts`` for the whole document the route produces. The
one Python re-implementation left is ``_json_escaped``, and it is deliberate: it
is the *refuted* claim — ``JSON.stringify`` alone, as the emitter used to be —
and nothing ships it.

The defect that put this here: ``JSON.stringify`` escapes nothing above U+001F,
PyYAML treats U+0085 as a line break, and a pasted employer name carrying one
produced a ``targeting.yaml`` the pipeline refuses to load — from first setup,
with nothing on screen to say why.

Nothing in this file carries a literal copy of one of those characters. Every one
of them is written as ``chr(...)``, for the same reason the TypeScript cases write
them as escapes: they are invisible, and that is what made the defect invisible.
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from venator.match.store import filters_version

REPOSITORY = Path(__file__).resolve().parents[2]
ADAPTER = REPOSITORY / "ui" / "server" / "onboarding" / "verify_profile.py"

#: The shipping emitter, behind a one-document-in, one-document-out seam.
EMITTER = REPOSITORY / "ui" / "tests" / "quote-scalars.ts"

#: The shipping employer edit — ``validateEmployer`` and ``addEmployers`` — behind
#: the same seam, so the document a Python case hashes is the document the route
#: writes rather than one assembled here to look like it.
EDITOR = REPOSITORY / "ui" / "tests" / "register-employers.ts"

#: ``writeProfile`` itself — the whole of ``POST /api/onboarding/profile`` below
#: the JSON parsing, load-back included — writing into a temporary Install.
WRITER = REPOSITORY / "ui" / "tests" / "write-profile.ts"

#: A Profile the loader reads: a name and a board registry.
LOADS = "\n".join(
    [
        "profile:",
        '  name: "mine"',
        "sources:",
        "  names:",
        '    ginkgobioworks: "Ginkgo Bioworks"',
        "  boards:",
        "    greenhouse:",
        '      - "ginkgobioworks"',
        "",
    ]
)

#: U+0085. A line break to PyYAML, above U+001F so JSON leaves it alone, and
#: invisible in every editor — which is the whole of why this was not caught.
NEL = chr(0x85)


def run_adapter(directory: Path) -> dict:
    """The adapter, run the way the dashboard runs it: one subprocess, one document."""
    finished = subprocess.run(
        [sys.executable, str(ADAPTER), "--directory", str(directory)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


def test_a_profile_the_loader_reads_is_reported_as_loading(tmp_path: Path) -> None:
    (tmp_path / "targeting.yaml").write_text(LOADS, encoding="utf-8")

    assert run_adapter(tmp_path) == {"outcome": "loads"}


def test_asking_writes_nothing(tmp_path: Path) -> None:
    """The whole point is that this runs before the rename, so it must not act."""
    (tmp_path / "targeting.yaml").write_text(LOADS, encoding="utf-8")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    run_adapter(tmp_path)

    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_a_document_the_loader_refuses_is_reported_as_refused(tmp_path: Path) -> None:
    # The reproduction, exactly: an employer display name a person pasted, carrying
    # one U+0085, written the way `JSON.stringify` alone would write it.
    document = 'profile:\n  name: "Acme' + NEL + '--- "\nsources:\n  names: {}\n'
    (tmp_path / "targeting.yaml").write_text(document, encoding="utf-8")

    answer = run_adapter(tmp_path)

    assert answer["outcome"] == "refused"
    # The sentence names the file, which is why the Node side logs it rather than
    # answering with it: this is a staging directory inside somebody's own
    # application data directory.
    assert "targeting.yaml" in answer["message"]


def test_a_directory_that_is_not_a_profile_is_refused_rather_than_crashing(tmp_path: Path) -> None:
    answer = run_adapter(tmp_path / "nothing-here")

    assert answer["outcome"] == "refused"


#: Every codepoint PyYAML will not read out of a document as itself, as
#: ``server/onboarding/yaml.ts: UNREADABLE_TO_THE_LOADER`` states it.
UNREADABLE = (
    [*range(0x7F, 0xA0)]  # non-printable to the reader; U+0085 is also a line break
    + [0x2028, 0x2029]  # line breaks the scanner keeps, and a plain key cannot hold
    + [0xFFFE, 0xFFFF]  # the two non-characters above U+FFFD
)


def _json_escaped(text: str) -> str:
    """``JSON.stringify``, as the emitter used to be, and nothing more.

    Escapes what is below U+0020, the two characters JSON must escape, and — since
    ES2019 — a lone surrogate, which has no UTF-8 encoding and would otherwise make
    the output not be text at all. Everything else it leaves alone, and that last
    clause is the whole defect.
    """
    escapes = {
        '"': '\\u0022',
        "\\": '\\u005c',
        chr(8): "\\b",
        chr(12): "\\f",
        chr(10): "\\n",
        chr(13): "\\r",
        chr(9): "\\t",
    }
    out = ['"']
    for character in text:
        if character in escapes:
            out.append(escapes[character])
        elif ord(character) < 0x20 or 0xD800 <= ord(character) <= 0xDFFF:
            out.append(f"\\u{ord(character):04x}")
        else:
            out.append(character)
    return "".join(out) + '"'


def _node() -> str:
    """The interpreter the emitter runs in.

    Failed rather than skipped, and deliberately: the sweep below is the only
    thing that says ``yaml.ts``'s escape set is complete, and a check that
    quietly stops running is the exact failure this file was rewritten to close.
    CI installs Node for the Python jobs for this reason.
    """
    node = shutil.which("node")
    if node is None:
        pytest.fail(
            "node is needed to run the emitter these cases certify: "
            "the escape set in ui/server/onboarding/yaml.ts is checked by running it, "
            "never by a second copy of it in Python"
        )
    return node


def _drive(script: Path, request: str) -> object:
    """One process, one JSON document in, one out — the seam the dashboard uses.

    ``encoding="utf-8"`` rather than ``text=True``, and it is not a preference.
    ``text=True`` decodes the child's stdout with ``locale.getencoding()``, which
    is **cp1252 on a Windows runner**, and what comes back here is a YAML scalar
    per Basic Multilingual Plane codepoint — every one the emitter does not
    escape is in the output literally. cp1252 has no mapping for 0x81, 0x8D or
    0x8F, so the decode raised inside ``subprocess``'s reader thread, ``stdout``
    came back ``None``, and 46 cases failed with a ``TypeError`` about ``NoneType``
    that says nothing about encodings at all. Node writes UTF-8 to a pipe on every
    platform, and ``JSON.stringify`` escapes a lone surrogate, so strict UTF-8 is
    exactly right on both ends.
    """
    finished = subprocess.run(
        [_node(), str(script)],
        input=request,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=300,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    # Said rather than left to `json.loads`: `None` here means the pipe could not
    # be decoded, and that is worth reading as a sentence rather than as a
    # `TypeError` about the shape of an argument.
    assert finished.stdout is not None, f"{script.name} produced no readable output"
    return json.loads(finished.stdout)


def emit(payloads: Sequence[str]) -> list[str]:
    """``quoteScalar`` itself, over a batch of payloads, in one spawn.

    Codepoints rather than text on the way in: a lone surrogate has no UTF-8
    encoding, so it could not cross a pipe as itself. What comes back is what
    the shipping emitter wrote.
    """
    request = json.dumps([[ord(character) for character in payload] for payload in payloads])
    answer = _drive(EMITTER, request)
    assert isinstance(answer, list) and len(answer) == len(payloads), answer
    return answer


#: Where a codepoint is put inside its payload, as (before, after). Four positions,
#: because a scalar's first and last character are where a quoting rule goes wrong.
POSITIONS: tuple[tuple[str, str], ...] = (("a", "b"), ("", ""), ("", "z"), ("z", ""))


def _payloads(codepoints: Sequence[int]) -> list[str]:
    return [before + chr(codepoint) + after for codepoint in codepoints for before, after in POSITIONS]


@functools.lru_cache(maxsize=None)
def _emitted_table(codepoints: tuple[int, ...]) -> dict[str, str]:
    """Every payload for these codepoints, emitted in one spawn and remembered.

    Cached on the whole tuple so a parametrized case pays for one process rather
    than one per parameter, while each codepoint still reports as its own case.
    """
    payloads = _payloads(codepoints)
    return dict(zip(payloads, emit(payloads), strict=True))


def _survives_json_escaping_alone(payload: str, *, as_key: bool) -> bool:
    """Whether ``JSON.stringify`` alone produces a scalar the loader reads back."""
    escaped = _json_escaped(payload)
    document = f"{escaped}: v\n" if as_key else f"k: {escaped}\n"
    try:
        loaded = yaml.safe_load(document)
    except yaml.YAMLError:
        return False  # Refused outright: the loud half of the defect.
    # Or it loads as something other than what was written: the silent half.
    return list(loaded) == [payload] if as_key else loaded["k"] == payload


@pytest.mark.parametrize("codepoint", UNREADABLE)
def test_json_escaping_alone_does_not_survive_the_loader(codepoint: int) -> None:
    """The claim ``yaml.ts`` used to make, refuted one codepoint at a time.

    The claim was that ``JSON.stringify`` produces a scalar YAML reads back
    character for character. A Profile writes strings in both positions —
    ``sources.names`` holds a board token as a *key* and the employer's name as a
    *value* — so the claim has to hold in both, and for each of these codepoints it
    holds in neither or in only one.
    """
    payload = f"a{chr(codepoint)}b"

    assert not (
        _survives_json_escaping_alone(payload, as_key=True)
        and _survives_json_escaping_alone(payload, as_key=False)
    )


def test_the_two_line_separators_break_a_key_while_a_value_survives_them() -> None:
    """Said outright, because it is why the set is not simply the reader's.

    U+2028 and U+2029 are readable characters: PyYAML preserves either one inside
    a double-quoted *value*. It refuses the document when one is inside a *key*,
    because a simple key may not span a line break — and a board token is emitted
    as a key under ``sources.names``. An escape set measured only against values
    would have missed both of them.
    """
    for codepoint in (0x2028, 0x2029):
        payload = f"a{chr(codepoint)}b"
        assert _survives_json_escaping_alone(payload, as_key=False)
        assert not _survives_json_escaping_alone(payload, as_key=True)


#: The interesting codepoints one at a time: every one the emitter escapes, plus
#: the boundaries either side of each range and one character above the BMP.
ROUND_TRIPPED = tuple([*UNREADABLE, 0x00, 0x1B, 0xA0, 0xD7FF, 0xE000, 0xFFFD, 0x10FFFF])


@pytest.mark.parametrize("codepoint", ROUND_TRIPPED)
def test_what_the_emitter_writes_now_round_trips_as_a_key_and_as_a_value(codepoint: int) -> None:
    emitted = _emitted_table(ROUND_TRIPPED)
    for before, after in POSITIONS:
        payload = before + chr(codepoint) + after
        scalar = emitted[payload]
        loaded = yaml.safe_load(f"{scalar}: {scalar}\n")
        assert list(loaded) == [payload], f"U+{codepoint:04X} as a key"
        assert loaded[payload] == payload, f"U+{codepoint:04X} as a value"


def test_the_whole_basic_multilingual_plane_round_trips_through_the_emitter() -> None:
    """The sweep the escape set was measured with, kept as what says it is complete.

    A codepoint the emitter leaves alone that PyYAML will not read is exactly the
    defect this fixes, and the only honest way to know the set is complete is to
    try every one of them — **through the emitter that ships**. This is one spawn
    of ``quoteScalar`` over all 65,536, in both positions, and it is the case that
    goes red when ``UNREADABLE_TO_THE_LOADER`` narrows by so much as one codepoint.
    """
    codepoints = [*range(0x0000, 0x10000)]
    payloads = [f"a{chr(codepoint)}b" for codepoint in codepoints]
    for codepoint, payload, scalar in zip(codepoints, payloads, emit(payloads), strict=True):
        loaded = yaml.safe_load(f"{scalar}: {scalar}\n")
        assert list(loaded) == [payload], f"U+{codepoint:04X}"
        assert loaded[payload] == payload, f"U+{codepoint:04X}"


#: The document the employer route edits: a Profile that exists and has no
#: employers in it, which is exactly the state the route exists to rescue.
EMPTY_REGISTRY = "\n".join(
    [
        "profile:",
        '  name: "mine"',
        "sources:",
        "  names: {}",
        "  boards: {}",
        "",
    ]
)

#: Employers the route accepts and that are still awkward to write down: a name
#: in another script, one with an astral character in it, one carrying a
#: non-breaking space, and board tokens that YAML 1.1 resolves to a boolean or a
#: null when they are written bare.
AWKWARD_BUT_ACCEPTED = [
    {"source": "greenhouse", "board": "ginkgobioworks", "name": "Ginkgo Bioworks"},
    {"source": "lever", "board": "no", "name": "Nō Kabushiki Kaisha"},
    {"source": "greenhouse", "board": "yes", "name": "Yes" + chr(0xA0) + "Industries"},
    {"source": "workday", "board": "acme~careers", "name": "Acme " + chr(0x1F3E2)},
    {"source": "greenhouse", "board": "null", "name": "Null Therapeutics"},
]


def register(targeting: str, boards: list[dict[str, str]]) -> dict:
    """The employer edit itself: ``validateEmployer`` then ``addEmployers``."""
    answer = _drive(EDITOR, json.dumps({"targeting": targeting, "boards": boards}))
    assert isinstance(answer, dict), answer
    return answer


def test_a_targeting_file_the_route_wrote_is_one_the_match_stage_can_hash(tmp_path: Path) -> None:
    """The gap the lone-surrogate defect lived in, closed from this side.

    The load-back answers a narrow question — does ``src/venator/profile/`` read
    this directory back — and a document can pass it and still be one no run can
    use. ``filters_version()`` re-serializes ``targeting.yaml`` through
    ``json.dumps(...).encode("utf-8")``, because ``sources.boards`` is excluded
    from the hash, and that is the *same file* this route writes. So what the
    route produces is hashed here as well as loaded, and neither claim stands in
    for the other.
    """
    answer = register(EMPTY_REGISTRY, AWKWARD_BUT_ACCEPTED)
    assert answer.get("added") == len(AWKWARD_BUT_ACCEPTED), answer
    written = answer["text"]
    (tmp_path / "targeting.yaml").write_text(written, encoding="utf-8")

    assert run_adapter(tmp_path) == {"outcome": "loads"}

    # Not a value anyone should assert on — it is a hash of this temporary file —
    # but computing it at all is the property: this raised before the input bound.
    stamp = filters_version(tmp_path / "constraints.yaml", tmp_path / "targeting.yaml")
    assert len(stamp) == 12

    # And every employer is really in there, under the name it was given.
    loaded = yaml.safe_load(written)
    for employer in AWKWARD_BUT_ACCEPTED:
        assert loaded["sources"]["names"][employer["board"]] == employer["name"]


def test_a_lone_surrogate_loads_and_still_disables_the_match_stage(tmp_path: Path) -> None:
    """Why the route refuses one at the door, said in the terms that made it a defect.

    A lone surrogate is a legal string in JavaScript and in Python and has no
    UTF-8 encoding at all. The emitter escapes it to ASCII, PyYAML reads the
    escape back, and the load-back therefore answers ``loads`` — while every
    ``match.run`` for that Profile fails from then on. ``ui`` refuses the input;
    this is the measurement that says refusing it is not fussiness.

    The document is written by hand rather than through the route, because the
    route will not produce one any more. That is the point of the case.
    """
    document = EMPTY_REGISTRY.replace(
        '  names: {}', '  names:\n    loneboard: "Lone\\ud800Surrogate"'
    )
    (tmp_path / "targeting.yaml").write_text(document, encoding="utf-8")

    assert run_adapter(tmp_path) == {"outcome": "loads"}

    with pytest.raises(UnicodeEncodeError):
        filters_version(tmp_path / "constraints.yaml", tmp_path / "targeting.yaml")


@pytest.mark.parametrize(
    "employer",
    [
        {"source": "greenhouse", "board": "loneboard", "name": "Lone\ud800Surrogate"},
        {"source": "greenhouse", "board": "lone\ud800board", "name": "Lone Surrogate"},
    ],
)
def test_the_route_will_not_write_a_lone_surrogate_into_a_profile(employer: dict[str, str]) -> None:
    """And the bound itself, asked of the shipping validator rather than of a copy.

    ``JSON.parse`` mints a lone surrogate from a crafted ``"\\ud800"`` escape, so
    this arrives by a hand-written request and never by a paste — which is why it
    is checked at all, and why it is checked in both fields.
    """
    answer = register(EMPTY_REGISTRY, [employer])

    assert answer.get("refused") == "invalid_profile", answer


#: The three files a Profile is made of, as the setup step sends them. The resume
#: carries the four contact fields ``resume/render.py`` reads with ``[]``.
SETUP_RESUME = {
    "name": "A Person",
    "contact": {
        "location": "Boston, MA",
        "phone": "555-0100",
        "email": "a@b.co",
        "linkedin": "linkedin.com/in/x",
    },
}

#: A targeting policy that enables a Hard Filter, so this is a Profile that filters
#: rather than one that passes every Posting unfiltered.
SETUP_TARGETING = {
    "search": {"terms": ["scientist"]},
    "filters": {"enabled": ["role_target"]},
    "role_target": {"titles": ["scientist"]},
}


def write_profile(home: Path, name: str, targeting: dict) -> dict:
    """``POST /api/onboarding/profile``, run into a temporary Install.

    ``ensure_ascii=True`` on the way in is what lets a lone surrogate cross the
    pipe at all: it has no UTF-8 encoding, and ``JSON.parse`` mints one from the
    ``\\ud800`` escape exactly as it would from a hand-written request.
    """
    request = json.dumps(
        {
            "home": str(home),
            "python": sys.executable,
            "name": name,
            "resume": SETUP_RESUME,
            "constraints": {"screening": {"how_heard": None}},
            "targeting": targeting,
        },
        ensure_ascii=True,
    )
    answer = _drive(WRITER, request)
    assert isinstance(answer, dict), answer
    return answer


def test_a_profile_the_setup_route_wrote_is_one_the_match_stage_can_hash(tmp_path: Path) -> None:
    """The same claim as the employer route's, for the route everybody meets first.

    ``POST /api/onboarding/profile`` writes a Profile's whole text, so it is the
    widest way for something the loader accepts and the Match stage cannot use to
    reach disk. The load-back is not that check and cannot be: it asks whether
    ``venator.profile`` reads the directory, which a Profile carrying a lone
    surrogate does.
    """
    written = write_profile(tmp_path, "setup", SETUP_TARGETING)
    assert "directory" in written, written
    directory = Path(written["directory"])
    assert sorted(written["files"]) == ["constraints.yaml", "resume.yaml", "targeting.yaml"]

    assert run_adapter(directory) == {"outcome": "loads"}

    # The half the load-back cannot answer. This raised before the input bound.
    stamp = filters_version(directory / "constraints.yaml", directory / "targeting.yaml")
    assert len(stamp) == 12


@pytest.mark.parametrize(
    ("where", "targeting"),
    [
        # A search term: ordinary Profile text, and the reproduction that was live
        # through this route after the employer route's bound was closed.
        ("a search term", {**SETUP_TARGETING, "search": {"terms": ["scientist\ud800"]}}),
        # A board token, which `sources.names` holds in *key* position.
        ("a key", {**SETUP_TARGETING, "sources": {"names": {"lone\ud800board": "Acme"}}}),
        # And an employer display name reached through this route rather than the
        # employer route, which is the same file by another door.
        ("a nested value", {**SETUP_TARGETING, "sources": {"names": {"acme": "Ac\ud800me"}}}),
    ],
)
def test_the_setup_route_will_not_write_a_lone_surrogate_anywhere_in_a_profile(
    tmp_path: Path, where: str, targeting: dict
) -> None:
    written = write_profile(tmp_path, "setup", targeting)

    assert written.get("refused") == "invalid_profile", f"{where}: {written}"
    # Refused before anything was created: the Profiles root is not even made.
    assert not (tmp_path / "profiles" / "setup").exists(), where


#: The plain words PyYAML resolves to something that is not text, which
#: ``server/onboarding/yaml.ts: RESOLVES_TO_NOT_TEXT`` covers.
YAML_ONE_ONE_WORDS = [
    spelling(word)
    for word in ("yes", "no", "on", "off", "true", "false", "null")
    for spelling in (str.lower, str.title, str.upper)
]


@pytest.mark.parametrize("word", YAML_ONE_ONE_WORDS)
def test_a_bare_yaml_one_one_word_is_not_the_key_that_was_written(word: str) -> None:
    loaded = yaml.safe_load(f"{word}: v")

    assert list(loaded) != [word]


def test_four_board_tokens_written_bare_collapse_into_three_keys() -> None:
    """Why this is a defect and not an inconvenience.

    ``sources.names`` is how Dedup resolves an ATS Posting's employer. A board
    with no entry there drops out of every duplicate group, so an employer lost
    at this step is the same role reaching the Owner twice, with nothing on
    screen to say why.
    """
    bare = yaml.safe_load("yes: A\nno: B\non: C\nnull: D\n")

    assert len(bare) == 3

    quoted = yaml.safe_load('"yes": A\n"no": B\n"on": C\n"null": D\n')

    assert list(quoted) == ["yes", "no", "on", "null"]
