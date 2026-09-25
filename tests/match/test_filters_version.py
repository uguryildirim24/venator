"""What moves ``filters_version``, and what deliberately does not.

A version that moves when it should not replays all stored Hard Filter Decisions
for nothing. A version that stands still when it should move leaves verdicts
wrong with no replay to correct them. Both failures are silent, so both are
pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from venator.match import store
from venator.match.store import filters_version

TARGETING = """\
# The Owner's search.
sources:
  names:
    benchling: Benchling
  boards:
    ashby:
      - benchling
filters:
  enabled:
    - role_target
  role_target:
    exclude:
      terms: [sales]
"""


@pytest.fixture()
def profile_files(tmp_path: Path) -> tuple[Path, Path]:
    constraints = tmp_path / "constraints.yaml"
    targeting = tmp_path / "targeting.yaml"
    constraints.write_text("work_authorization:\n  status: F-1\n", encoding="utf-8")
    targeting.write_text(TARGETING, encoding="utf-8")
    return constraints, targeting


def test_registering_another_board_does_not_replay_the_corpus(
    profile_files: tuple[Path, Path],
) -> None:
    """The pathology the exclusion exists for.

    ``sources.boards`` is the list of boards Discover polls. No Hard Filter and
    no Dedup reads it, so adding one must cost nothing.
    """
    constraints, targeting = profile_files
    before = filters_version(constraints, targeting)
    targeting.write_text(
        TARGETING.replace("      - benchling\n", "      - benchling\n      - asimov\n"),
        encoding="utf-8",
    )

    assert filters_version(constraints, targeting) == before


def test_naming_another_employer_does_replay_the_corpus(
    profile_files: tuple[Path, Path],
) -> None:
    """``sources.names`` is a decision input, so an edit to it is a replay.

    Dedup resolves an ATS Posting's employer through this registry
    (``dedup.employer_display``); a board with no entry has no employer at all
    and every Posting from it drops out of every duplicate group. Deleting
    ``sources.names["benchling"]`` moved verdicts in an earlier corpus while Dedup ranked the whole corpus, and moves 0 under the
    shipping order only because no ATS Posting currently survives the fit
    filters into a group. What the registry decides is what Dedup *can* merge,
    which is not a property of one week's corpus, so it belongs in the hash.
    """
    constraints, targeting = profile_files
    before = filters_version(constraints, targeting)
    targeting.write_text(
        TARGETING.replace("    benchling: Benchling\n", "    benchling: Benchling Inc.\n"),
        encoding="utf-8",
    )
    renamed = filters_version(constraints, targeting)
    targeting.write_text(TARGETING.replace("  names:\n", "  names: {}\n#"), encoding="utf-8")

    assert renamed != before
    assert filters_version(constraints, targeting) not in {before, renamed}


def test_editing_a_hard_filter_replays_the_corpus(profile_files: tuple[Path, Path]) -> None:
    constraints, targeting = profile_files
    before = filters_version(constraints, targeting)
    targeting.write_text(TARGETING.replace("[sales]", "[sales, recruiting]"), encoding="utf-8")

    assert filters_version(constraints, targeting) != before


def test_a_comment_or_a_key_reorder_no_longer_replays_the_corpus(
    profile_files: tuple[Path, Path],
) -> None:
    """A file with an exclusion is hashed as content, so its wording is free.

    That is the same property the exclusion buys, applied consistently: the
    hash is over what a decision reads, and a decision does not read the
    Owner's comments or the order they happened to write two blocks in.
    """
    constraints, targeting = profile_files
    before = filters_version(constraints, targeting)
    body = TARGETING.split("\n", 1)[1]
    filters_block = body[body.index("filters:") :]
    sources_block = body[: body.index("filters:")]
    targeting.write_text(f"# a different note\n{filters_block}{sources_block}", encoding="utf-8")

    assert filters_version(constraints, targeting) == before


def test_constraints_are_still_hashed_as_their_bytes(profile_files: tuple[Path, Path]) -> None:
    """Only the targeting file has an exclusion, and it is keyed by position.

    Nothing in ``constraints.yaml`` is excluded — including a ``sources:`` block
    if one ever appeared there — so a comment in it still replays, exactly as
    before. This is what keeps the exclusion honest: it is a statement about one
    argument of one function, not about any key named ``boards`` anywhere.
    """
    constraints, targeting = profile_files
    before = filters_version(constraints, targeting)
    constraints.write_text(
        "# a note\nwork_authorization:\n  status: F-1\n", encoding="utf-8"
    )

    assert filters_version(constraints, targeting) != before


def test_a_targeting_file_hashed_as_the_constraints_file_keeps_its_boards(
    profile_files: tuple[Path, Path],
) -> None:
    """Exclusions are keyed by argument position, not by the filename."""
    _, targeting = profile_files
    before = filters_version(targeting)
    targeting.write_text(
        TARGETING.replace("      - benchling\n", "      - benchling\n      - asimov\n"),
        encoding="utf-8",
    )

    assert filters_version(targeting) != before


def test_yaml_that_cannot_be_read_falls_back_to_its_raw_bytes(tmp_path: Path) -> None:
    """An unparseable Profile is never silently hashed as nothing.

    Dropping a block requires understanding the file. When that fails — invalid
    YAML, undecodable bytes, or a document that is not a mapping — the whole
    file is hashed exactly as it was before this exclusion existed, which is
    the conservative direction: every edit replays.
    """
    constraints = tmp_path / "constraints.yaml"
    constraints.write_text("work_authorization:\n  status: F-1\n", encoding="utf-8")
    broken = tmp_path / "targeting.yaml"

    broken.write_text("filters: [unclosed\n", encoding="utf-8")
    invalid_yaml = filters_version(constraints, broken)
    broken.write_text("filters: [unclosed   \n", encoding="utf-8")
    assert filters_version(constraints, broken) != invalid_yaml

    broken.write_bytes(b"\xff\xfe not utf-8 at all")
    undecodable = filters_version(constraints, broken)
    broken.write_bytes(b"\xff\xfe not utf-8 either")
    assert filters_version(constraints, broken) != undecodable

    broken.write_text("- a list, not a mapping\n", encoding="utf-8")
    sequence = filters_version(constraints, broken)
    broken.write_text("- a different list\n", encoding="utf-8")
    assert filters_version(constraints, broken) != sequence


def test_a_named_but_absent_file_hashes_as_empty_and_adding_it_replays(
    tmp_path: Path,
) -> None:
    """The property the exclusion had to survive, on both arguments.

    A Profile file that does not exist yet is not an error — an absent field
    configures nothing (CONTRACTS.md). It hashes as empty, and writing it later
    moves the version, so the corpus replays under rules that now exist.
    """
    constraints = tmp_path / "constraints.yaml"
    targeting = tmp_path / "targeting.yaml"
    empty = filters_version(constraints, targeting)

    targeting.write_text(TARGETING, encoding="utf-8")
    with_targeting = filters_version(constraints, targeting)
    constraints.write_text("work_authorization:\n  status: F-1\n", encoding="utf-8")

    assert with_targeting != empty
    assert filters_version(constraints, targeting) not in {empty, with_targeting}


def test_minting_a_profile_identifier_does_not_replay_the_corpus(
    profile_files: tuple[Path, Path],
) -> None:
    """``profile.id`` is provenance, not a verdict, so stamping one is free.

    A Profile that has gone without an identifier gets one the day its Owner
    mints it. Every verdict it produces afterwards is byte-identical to the one
    before — the identifier says who decided, never what was decided — so
    hashing it would send all stored Hard Filter Decisions back through a replay
    and restale every ``llm_score`` row to reproduce exactly what was already
    there, on the Owner's own subscription.
    """
    constraints, targeting = profile_files
    before = filters_version(constraints, targeting)
    targeting.write_text(
        f"profile:\n  name: ada\n  id: 6f1c9d2b4a8e47f0b3c5d7e9a1b2c3d4\n{TARGETING}",
        encoding="utf-8",
    )
    stamped = filters_version(constraints, targeting)
    targeting.write_text(
        f"profile:\n  name: ada\n  id: 0000000000000000000000000000beef\n{TARGETING}",
        encoding="utf-8",
    )

    assert stamped == filters_version(constraints, targeting)
    assert stamped != before, "profile.name is still hashed; only the identifier is excluded"


def test_excluding_the_identifier_moved_nothing_for_a_profile_without_one(
    profile_files: tuple[Path, Path], monkeypatch
) -> None:
    """Adding the exclusion owed no replay to any Profile already in the field.

    This is what made the change additive. Every Profile that existed before
    ``profile.id`` did simply has no such key, and pruning a key that is not
    there is a no-op — so the version under the shipped exclusions is the same
    version those Profiles were already stamped with, and nobody's corpus was
    replayed to say so. Asserted against the exclusions as they
    stood *before* the identifier joined them.
    """
    constraints, targeting = profile_files
    assert "profile:" not in targeting.read_text(encoding="utf-8")
    shipped = filters_version(constraints, targeting)

    monkeypatch.setattr(store, "NON_DECIDING_BLOCKS", ((), ("sources.boards",)))

    assert filters_version(constraints, targeting) == shipped
