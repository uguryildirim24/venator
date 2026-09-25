"""Read and append the Git-transport JSONL stores used by matching."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import yaml

from venator.profile.claim import (
    STORE_OWNER_FILE,
    claim_store,
    report_stray_rows,
    store_owner,
)
from venator.profile.claim import rows_from_other_profiles as _rows_from_other_profiles

#: The Track store stamps the same filename with the same identifier
#: (``venator.track.store``); the mechanism is shared so the two cannot drift.
DECISIONS_OWNER_FILE = STORE_OWNER_FILE


def iter_jsonl(directory: Path) -> Iterable[dict]:
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.jsonl")):
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"invalid JSON in {path}:{line_number}: {error.msg}") from error
                if not isinstance(value, dict):
                    raise ValueError(f"expected a JSON object in {path}:{line_number}")
                yield value


def load_postings(postings_dir: Path) -> list[dict]:
    """Use discovery's reader for full rows and compact verification updates."""
    from venator.discover.store import load_postings as current_postings
    return current_postings(postings_dir)


def load_latest_decisions(decisions_dir: Path) -> dict[tuple[str, str], dict]:
    """Return effective state: the last appended decision for each Posting/stage."""
    latest: dict[tuple[str, str], dict] = {}
    for decision in iter_jsonl(decisions_dir):
        posting_key = decision.get("posting_key")
        stage = decision.get("stage")
        if isinstance(posting_key, str) and isinstance(stage, str):
            latest[(posting_key, stage)] = decision
    return latest


def hard_decision_keys(decisions_dir: Path, filters_version: str) -> set[str]:
    """Return Posting keys already hard-filtered under one Profile version."""
    return {
        decision["posting_key"]
        for decision in iter_jsonl(decisions_dir)
        if decision.get("stage") == "hard_filter"
        and decision.get("filters_version") == filters_version
        and isinstance(decision.get("posting_key"), str)
    }


def append_decisions(decisions_dir: Path, decisions: Iterable[dict], *, day: date | None = None) -> int:
    values = list(decisions)
    if not values:
        return 0
    decisions_dir.mkdir(parents=True, exist_ok=True)
    output = decisions_dir / f"{(day or date.today()).isoformat()}.jsonl"
    with output.open("a", encoding="utf-8", newline="\n") as destination:
        for decision in values:
            destination.write(json.dumps(decision, ensure_ascii=False) + "\n")
    return len(values)


# Bump whenever filter CODE changes behavior, so decided Postings get replayed —
# the version must reflect the rules, not only the Profile files.
#
# 20 removes the retired trained qualification-model delegation path.
# 21 adds the SmartRecruiters Source; 22 removes the location Hard Filter.
FILTERS_REVISION = b"22-smartrecruiters-no-location-filter"

#: Blocks that live in a hashed Profile file but reach no decision, keyed by
#: **which positional argument** of ``filters_version`` the file is — not by its
#: name, so ``--constraints /elsewhere/targeting.yaml`` cannot silently get the
#: targeting treatment. Index 0 is the constraints file, index 1 the targeting
#: file.
#: Each entry is a dotted path, matched nested: ``sources.boards`` names the
#: ``boards`` key inside the top-level ``sources`` mapping and nothing else.
#:
#: Today the one exclusion is ``targeting.yaml: sources.boards`` — the list of
#: which ATS boards to poll. Discover reads it; no Hard Filter and no Dedup
#: does. Hashing the file's raw bytes made registering one new board
#: replay all stored Hard Filter Decisions every time anyone added a board.
#:
#: ``sources.names`` is deliberately **inside** the hash. Dedup resolves an ATS
#: Posting's employer through it (``dedup.employer_display``), and a board with
#: no entry there has no employer at all, so every Posting from that board drops
#: out of every duplicate group. That is a decision input by construction, and
#: it is hashed like one. A changed employer name can change Dedup groups
#: even if today no Posting from that board survives the fit filters.
#: The registry decides what Dedup can
#: merge whether or not this week's corpus happens to exercise it, and the next
#: aggregator listing of an ATS job is what it decides.
#:
#: So, stated both ways, and stated smaller than it once was: this exclusion
#: exists so that registering another board need not replay the corpus, and in
#: practice it almost never buys that any more. ``names`` is keyed by **board
#: token**, not by employer, so a newly registered board needs its own ``names``
#: entry — even for an employer already named under a different token, and the
#: loader now refuses the registry outright if it does not get one. That entry
#: is inside the hash. Every board registration therefore moves
#: ``filters_version`` and costs a full replay of the corpus.
#:
#: What the exclusion still buys is narrow and worth naming exactly: reordering
#: or recommenting ``boards:``, and removing a board while leaving its name in
#: place, stay free. Whether that is worth the asymmetry — or whether the
#: harvest-and-merge workflow wants a different arrangement — is open, and
#: hashing ``names`` is not the part to give up: Dedup resolves employers
#: through it, so a rename genuinely changes verdicts. The rule for adding an
#: entry here is the inverse of the rule for adding a file to the hash: a block
#: belongs here only while nothing that decides anything reads it, at any depth.
#:
#: ``profile.id`` is excluded for a different reason from ``sources.boards``,
#: and the difference is worth stating. It is not that no stage reads it —
#: ``match.run`` does, and stamps it on every row it
#: appends. It is that nothing it reaches is a *verdict*: the identifier records
#: which Profile produced a Filter Decision, never whether the Posting passed.
#: Hashing it would mean that minting an identifier for a Profile that had gone
#: without one replayed all stored Hard Filter Decisions to produce
#: byte-identical verdicts. The rule this
#: file states for ``sources.boards`` is "nothing that decides anything reads
#: it"; ``profile.id`` satisfies it in the sense that matters, because what it
#: feeds is the row's provenance, not the decision in it.
NON_DECIDING_BLOCKS: tuple[tuple[str, ...], ...] = (
    (),  # constraints.yaml
    ("sources.boards", "profile.id"),  # targeting.yaml
)


def _without(value: object, path: tuple[str, ...]) -> object:
    """``value`` with the nested key at ``path`` removed, non-destructively.

    A path that does not lead anywhere leaves the value alone: a Profile that
    never wrote the block is the same input as one that wrote it and had it
    dropped, which is what keeps the hash stable across both.
    """
    if not path or not isinstance(value, dict) or path[0] not in value:
        return value
    head, rest = path[0], path[1:]
    pruned = dict(value)
    if rest:
        pruned[head] = _without(pruned[head], rest)
    else:
        del pruned[head]
    return pruned


def _hashed_bytes(path: Path, excluded: tuple[str, ...]) -> bytes:
    """The part of a Profile file that decides anything, canonically serialized.

    Raw bytes for a file with no exclusion — and raw bytes as the fallback
    whenever the file is absent, unreadable as YAML, or not a mapping, so a
    named-but-absent file still hashes as empty and adding it later still
    replays. A file *with* an exclusion is hashed as its remaining content
    re-serialized canonically, so reordering or recommenting the kept blocks
    does not replay the corpus either — the same property, applied
    consistently. The canonical form is JSON with sorted keys rather than
    ``yaml.safe_dump(sort_keys=True)``, which silently falls back to insertion
    order when two keys are not comparable to each other.
    """
    if not path.exists():
        return b""
    raw = path.read_bytes()
    if not excluded:
        return raw
    try:
        parsed = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return raw
    if not isinstance(parsed, dict):
        return raw
    kept: object = parsed
    for dotted in excluded:
        kept = _without(kept, tuple(dotted.split(".")))
    return json.dumps(kept, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


def filters_version(*paths: Path, promotion_state_revision: str = "", jev_release_hash: str = "") -> str:
    """Stamp a Filter Decision with everything that produced it.

    The Hard Filters read their wording from the Profile's targeting policy, so
    hashing only constraints.yaml would let a targeting edit change what gets
    killed while the version stayed put — and the replay that should follow
    would never run. So every file whose content reaches a decision is hashed
    here: the caller passes the Profile files. A named-but-absent file hashes as empty, so adding it later still replays.

    The exclusions are content rather than files: the blocks named in
    ``NON_DECIDING_BLOCKS`` are dropped before hashing, keyed by which
    positional argument the file is. Today that is ``targeting.yaml:
    sources.boards``, the list of boards to poll, which only Discover reads —
    so registering another board for an already-named employer costs nothing.
    ``sources.names`` stays in the hash because Dedup resolves employers
    through it, so adding an employer display name still replays the corpus,
    which is correct: it changes what Dedup can merge. The claim stays literal
    in the direction that matters — nothing a decision reads is left out.

    The mirror of that claim is what a Filter Decision *records*. The row's
    ``facts`` — the token each Hard Filter derives on a pass — and its
    ``profile_id`` are both produced by this pipeline rather than read by it,
    and neither is hashed. Adding a recording field to this hash would replay
    all stored Hard Filter Decisions to produce byte-identical verdicts. The test for whether something belongs here
    is not "does the code touch it" but "can it change a verdict".
    """
    digest = hashlib.sha256(FILTERS_REVISION)
    if promotion_state_revision:
        digest.update(b"\0promotion_state_revision\0" + promotion_state_revision.encode("ascii"))
    if jev_release_hash:
        digest.update(b"\0jev_release_hash\0" + jev_release_hash.encode("ascii"))
    for index, path in enumerate(paths):
        excluded = NON_DECIDING_BLOCKS[index] if index < len(NON_DECIDING_BLOCKS) else ()
        digest.update(b"\0" + path.name.encode("utf-8") + b"\0")
        digest.update(_hashed_bytes(path, excluded))
    if paths:
        resume_path = paths[0].parent / "resume.yaml"
        digest.update(b"\0resume.yaml\0" + _hashed_bytes(resume_path, ()))
    return digest.hexdigest()[:12]


def rows_from_other_profiles(decisions_dir: Path, identifier: str) -> dict[str, int]:
    """Count appended Filter Decisions that name a Profile other than ``identifier``.

    A row written before ``profile_id`` existed carries no identifier, and that
    is **unknown, never a mismatch** — it is skipped rather than counted against
    anything. ADR-0001 makes ``data/`` append-only, so those rows are permanent
    and must stay ordinary.
    """
    return _rows_from_other_profiles(iter_jsonl(decisions_dir), identifier)


def claim_decisions_dir(decisions_dir: Path, identifier: str) -> None:
    """Refuse to interleave two Profiles' Filter Decisions in one store.

    ``--profile`` exists so one person can keep several Profiles, and two of them
    writing one ``data/decisions`` would interleave: ``load_latest_decisions``
    keys on (Posting, stage) alone and would return whichever Profile ran last —
    a Posting killed by one search silently resurrected by the other. Rather than
    mix them, the directory records the Profile that owns it and refuses another.
    **This stamp is the enforcement**, and it stays the enforcement now that rows
    carry a ``profile_id`` too. The identifier on a row is evidence, not a second
    lock: a store is single-Profile by construction, so the directory is the
    right granularity to refuse at, and two locks that can disagree with each
    other are worse than one that cannot.

    It is the Profile's ``identifier`` that is compared, not its name, so
    renaming ``profiles/<name>/`` does not lock the Owner out of their own
    decisions. A Profile with no minted ``profile.id`` identifies as its name,
    which is exactly what this file already recorded before the field existed —
    so every existing store keeps matching without being touched.

    Appended rows that name a *different* Profile are reported as a warning on
    stderr and nothing more. That is what a mis-pointed ``--decisions-dir``
    produces, and what a data directory copied or restored from a backup can
    produce, and it deserves to be **said** rather than enforced twice: no row
    is dropped, filtered, or rewritten on account of it, and effective state
    still reads every one of them.
    """
    owner = decisions_dir_owner(decisions_dir)
    if owner is not None:
        if owner != identifier:
            stamp = decisions_dir / DECISIONS_OWNER_FILE
            raise ValueError(
                f"{decisions_dir} holds Filter Decisions for Profile {owner!r}, and effective "
                f"state is keyed by Posting and stage alone — running {identifier!r} against it "
                "would interleave two Profiles' decisions and the effective state would be "
                "whichever ran last. Give this Profile its own store, e.g. --decisions-dir "
                f"data/{identifier}/decisions --postings-dir data/{identifier}/postings (or "
                f"delete {stamp} if {owner!r} was renamed)"
            )
    else:
        claim_store(decisions_dir, identifier)

    # Only once the claim has accepted this Profile: a run that is about to be
    # refused should hear the refusal, not a warning ahead of it.
    _report_stray_rows(decisions_dir, identifier)


def verify_decisions_dir(decisions_dir: Path, identifier: str) -> None:
    """Refuse to *read* one Profile's Filter Decisions as another Profile's.

    The write path has always claimed a decisions store
    (``claim_decisions_dir``); the read paths did not, and that asymmetry made
    the wrong answer the silent one. A read run with ``--profile`` naming one
    Profile against another Profile's store answered happily — measured, 34
    Postings where the owning Profile's own answer was 29 — because nothing on
    the way in compared the two. A read that
    is about to describe somebody else's search as this one's must stop and say
    both names, exactly as a write does.

    This is the *same* mechanism, not a second one: it compares the Profile
    identifier recorded in ``data/decisions/.profile``, which is what the claim
    already writes and already compares. Three consequences follow from reusing
    it rather than inventing a check of its own.

    **A read never claims.** An unstamped store is left unstamped: reading is
    not owning, and a ``view.build`` or a ``track.status`` that stamped a
    directory would decide, silently and as a side effect of a read-only
    command, which Profile a store belongs to.

    **An unclaimed store is unknown, not a mismatch.** No stamp means the
    question has not been answered, and a read proceeds — the same rule
    ``profile_id`` follows on the row (``coordination/CONTRACTS.md``). Any store
    written before the claim existed keeps being readable, with no flag day and
    nothing to migrate (ADR-0001).

    **Rows are evidence, never the lock.** A row naming another Profile is
    reported and nothing more, here as on the write path. Refusing a read on the
    strength of a row would be the second lock the contract rules out, and it
    would make a store restored from a backup unreadable rather than suspect.
    """
    owner = decisions_dir_owner(decisions_dir)
    if owner is not None and owner != identifier:
        stamp = decisions_dir / DECISIONS_OWNER_FILE
        raise ValueError(
            f"{decisions_dir} holds Filter Decisions for Profile {owner!r}, so reading it as "
            f"{identifier!r} would report another Profile's Filter Decisions as this one's — "
            "effective state is keyed by Posting and stage alone and carries no Profile. Point "
            f"at this Profile's own store, e.g. --decisions-dir data/{identifier}/decisions "
            f"--postings-dir data/{identifier}/postings (or delete {stamp} if {owner!r} was "
            "renamed)"
        )
    _report_stray_rows(decisions_dir, identifier)


def decisions_dir_owner(decisions_dir: Path) -> str | None:
    """The Profile identifier a decisions store is claimed by, or None if unclaimed.

    An empty stamp reads as unclaimed rather than as a Profile with a blank
    name: a blank identifier would match nothing and refuse everything.
    """
    return store_owner(decisions_dir)


def _report_stray_rows(decisions_dir: Path, identifier: str) -> None:
    """Say out loud that a store holds another Profile's Filter Decisions.

    The owner is re-read rather than assumed, because this is reached from the
    read path too, where an *unclaimed* store is the ordinary case: describing
    one as claimed would tell the person checking the opposite of the truth.
    """
    report_stray_rows(
        decisions_dir,
        identifier,
        rows_from_other_profiles(decisions_dir, identifier),
        owner=store_owner(decisions_dir),
        flag="--decisions-dir",
    )
