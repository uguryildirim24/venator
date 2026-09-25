"""Which Profile produced a Filter Decision, recorded on the row itself.

The enforcement that stops two Profiles interleaving is still the directory
claim (``data/decisions/.profile``), and these tests are careful not to turn the
row's ``profile_id`` into a second lock that could disagree with it. What the
field buys is narrower: a mis-pointed ``--decisions-dir`` becomes *detectable*
rather than trusted, and a row stays self-describing once the Install's data
directory outlives the checkout that wrote it (ADR-0002, as amended).

``data/`` is append-only (ADR-0001), so the 700-odd rows written before the
field existed are permanent and unstamped. Every test here treats a missing
value as **unknown** — never as a mismatch, never as a default Profile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.match.run import run
from venator.match.store import claim_decisions_dir, load_latest_decisions
from venator.profile import Profile, load_profile, new_profile_identifier

IDENTIFIER = "6f1c9d2b4a8e47f0b3c5d7e9a1b2c3d4"


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def profile_at(directory: Path, targeting: str = "") -> Profile:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "targeting.yaml").write_text(targeting, encoding="utf-8")
    return load_profile(directory)


def decisions_in(decisions_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in sorted(decisions_dir.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_a_new_decision_records_the_profile_that_produced_it(tmp_path: Path) -> None:
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(postings_dir / "2026-08-18.jsonl", [{"key": "p:1", "title": "One"}])
    profile = profile_at(
        tmp_path / "profiles" / "ada", f"profile:\n  name: ada\n  id: {IDENTIFIER}\n"
    )

    run(postings_dir, decisions_dir, profile=profile)

    assert [row["profile_id"] for row in decisions_in(decisions_dir)] == [IDENTIFIER]


def test_a_profile_with_no_identifier_falls_back_to_its_name(tmp_path: Path) -> None:
    """The fallback is what kept the change additive — no flag day for anyone.

    A Profile written before ``profile.id`` existed identifies as its name,
    which is exactly what the directory claim already recorded, so an existing
    store keeps matching without being touched.
    """
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(postings_dir / "2026-08-18.jsonl", [{"key": "p:1", "title": "One"}])
    profile = profile_at(tmp_path / "profiles" / "ada", "profile:\n  name: ada\n")

    run(postings_dir, decisions_dir, profile=profile)

    assert profile.identifier == "ada"
    assert [row["profile_id"] for row in decisions_in(decisions_dir)] == ["ada"]
    assert (decisions_dir / ".profile").read_text(encoding="utf-8").strip() == "ada"


def test_renaming_a_profile_directory_does_not_lock_the_owner_out(tmp_path: Path) -> None:
    """The claim compares identifiers, so the human label stays free to change.

    This is the reason the identifier is opaque rather than the directory name:
    ADR-0002 makes the data directory the boundary, and it can be copied,
    synced and restored well away from whatever the Profile directory is called
    this week.
    """
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(postings_dir / "2026-08-18.jsonl", [{"key": "p:1", "title": "One"}])
    before = profile_at(
        tmp_path / "profiles" / "ada", f"profile:\n  name: ada\n  id: {IDENTIFIER}\n"
    )
    run(postings_dir, decisions_dir, profile=before)

    renamed = profile_at(
        tmp_path / "profiles" / "ada-2027", f"profile:\n  name: ada-2027\n  id: {IDENTIFIER}\n"
    )
    run(postings_dir, decisions_dir, profile=renamed)

    assert {row["profile_id"] for row in decisions_in(decisions_dir)} == {IDENTIFIER}


def test_an_unstamped_row_still_loads_and_still_decides_effective_state(tmp_path: Path) -> None:
    """A row from before the field existed is ordinary, not suspect.

    It loads, it participates in effective state, and — being unstamped — it is
    superseded by a later decision for the same Posting and stage exactly as it
    always was. ``load_latest_decisions`` keeps keying on (Posting, stage)
    alone: putting the Profile in that key would silently resurrect superseded
    decisions the moment one directory did hold two Profiles.
    """
    decisions_dir = tmp_path / "decisions"
    write_jsonl(
        decisions_dir / "2026-08-18.jsonl",
        [
            {"posting_key": "p:1", "stage": "hard_filter", "verdict": "kill", "rule": "location"},
            {
                "posting_key": "p:1",
                "stage": "hard_filter",
                "verdict": "pass",
                "rule": None,
                "profile_id": IDENTIFIER,
            },
            {"posting_key": "p:2", "stage": "hard_filter", "verdict": "pass", "rule": None},
        ],
    )

    latest = load_latest_decisions(decisions_dir)

    assert latest[("p:1", "hard_filter")]["verdict"] == "pass"
    assert latest[("p:2", "hard_filter")]["verdict"] == "pass"
    assert "profile_id" not in latest[("p:2", "hard_filter")]


def test_an_unstamped_row_is_unknown_rather_than_a_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole corpus is unstamped, so this is the common case, not an edge one."""
    decisions_dir = tmp_path / "decisions"
    write_jsonl(
        decisions_dir / "2026-08-18.jsonl",
        [{"posting_key": "p:1", "stage": "hard_filter", "verdict": "pass"}],
    )

    claim_decisions_dir(decisions_dir, IDENTIFIER)

    assert capsys.readouterr().err == ""


def test_a_row_from_another_profile_is_reported_not_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mis-pointed ``--decisions-dir`` is said out loud, and nothing else.

    Deliberately not an error: the directory claim is the enforcement, and a
    second lock that can disagree with it is worse than one that cannot. The
    row is not filtered, not rewritten, and still counts toward effective state
    — ``data/`` is append-only, and a store restored from a backup must stay
    readable.
    """
    decisions_dir = tmp_path / "decisions"
    write_jsonl(
        decisions_dir / "2026-08-18.jsonl",
        [
            {
                "posting_key": "p:1",
                "stage": "hard_filter",
                "verdict": "pass",
                "profile_id": "another-profile",
            },
            {"posting_key": "p:2", "stage": "hard_filter", "verdict": "pass"},
        ],
    )
    (decisions_dir / ".profile").write_text(IDENTIFIER + "\n", encoding="utf-8")

    claim_decisions_dir(decisions_dir, IDENTIFIER)

    warning = capsys.readouterr().err
    assert "WARNING" in warning
    assert "1 by Profile 'another-profile'" in warning
    assert IDENTIFIER in warning
    assert "No row has been dropped or changed" in warning
    assert len(load_latest_decisions(decisions_dir)) == 2


def test_a_second_profile_is_still_refused_by_the_directory_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The enforcement did not move: it is still the claim, keyed on identifiers.

    And the refusal is the only thing said. The rows already in the store were
    written by the *other* Profile, so a stray-row warning would fire here too
    — ahead of the error, describing the same situation in weaker terms. The
    claim speaks first and alone.
    """
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(postings_dir / "2026-08-18.jsonl", [{"key": "p:1", "title": "One"}])
    first = profile_at(tmp_path / "profiles" / "ada", f"profile:\n  name: ada\n  id: {IDENTIFIER}\n")
    second = profile_at(
        tmp_path / "profiles" / "grace", "profile:\n  name: grace\n  id: 9f8e7d6c5b4a39281706\n"
    )

    run(postings_dir, decisions_dir, profile=first)
    with pytest.raises(ValueError) as error:
        run(postings_dir, decisions_dir, profile=second)

    message = str(error.value)
    assert f"holds Filter Decisions for Profile '{IDENTIFIER}'" in message
    assert "--decisions-dir" in message
    assert "WARNING" not in capsys.readouterr().err


def test_an_identifier_yaml_reads_as_a_number_is_still_that_identifier(tmp_path: Path) -> None:
    """``id: 4815162342`` is a string to whoever wrote it, whatever YAML types it as.

    A minted hex identifier can draw no letters, too — rarely, but a Profile
    that fails to load once in a few million setups is not a good trade for
    strictness about a value whose only purpose is to be opaque.
    """
    profile = profile_at(tmp_path / "profiles" / "ada", "profile:\n  name: ada\n  id: 4815162342\n")

    assert profile.identifier == "4815162342"


def test_a_minted_identifier_is_opaque_and_derived_from_nothing() -> None:
    """It must not be personal: ``data/`` is committed, synced and backed up.

    And it identifies a Profile *within* one Install, never a person across
    Installs — two Installs never share a store, so this value is never
    compared between them.
    """
    minted = {new_profile_identifier() for _ in range(64)}

    assert len(minted) == 64
    assert all(len(value) == 32 and value.isalnum() and value.islower() for value in minted)


@pytest.mark.parametrize("blank", ["", "\n", "   \n"])
def test_a_blank_stamp_is_unclaimed_and_the_first_profile_claims_it(
    tmp_path: Path, blank: str
) -> None:
    """An unclaimed store is claimable — pinning a behaviour that changed.

    A ``.profile`` that exists but holds nothing reads as unclaimed, because a
    blank identifier would match no Profile and refuse every one of them. What
    follows from that was not obvious and was not written down: the store is now
    *claimed by the first Profile that writes to it*, exactly as an absent stamp
    is, and a second Profile is then refused.

    Before, the emptiness was permanent. The stamp existed, so nothing rewrote
    it; the identifier was blank, so nothing was ever compared against it; and
    every Profile could keep appending to the same store forever — the one
    outcome the claim exists to prevent, reachable by truncating one file. The
    new behaviour is the safe direction, and this test is what stops it drifting
    back to either the old fail-open or an unrecoverable store that no Profile
    can ever claim.
    """
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(postings_dir / "2026-08-18.jsonl", [{"key": "p:1", "title": "One"}])
    decisions_dir.mkdir(parents=True)
    (decisions_dir / ".profile").write_text(blank, encoding="utf-8")
    first = profile_at(tmp_path / "profiles" / "ada", f"profile:\n  name: ada\n  id: {IDENTIFIER}\n")
    second = profile_at(
        tmp_path / "profiles" / "grace", "profile:\n  name: grace\n  id: 9f8e7d6c5b4a39281706\n"
    )

    claim_decisions_dir(decisions_dir, first.identifier)

    assert (decisions_dir / ".profile").read_text(encoding="utf-8").strip() == IDENTIFIER

    with pytest.raises(ValueError) as error:
        claim_decisions_dir(decisions_dir, second.identifier)

    assert f"holds Filter Decisions for Profile '{IDENTIFIER}'" in str(error.value)
