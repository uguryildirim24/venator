"""Local application actions used by the desktop app. Never submits a form."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
import sys
from collections.abc import Mapping
from contextlib import closing, redirect_stdout
from pathlib import Path

from venator.paths import resolve_store_paths
from venator.profile import add_profile_argument, profile_from_arguments
from venator.profile.claim import store_owner
from venator.secrets import scrub

FILES = frozenset({"resume.pdf", "resume.txt", "letter.txt", "letter.pdf"})
ACTIONS = ("status", "file", "apply", "edit", "save", "dismiss", "restore", "applied", "fill", "answers", "answer", "retract", "replace", "promote")


def application_directory(stores: dict, profile_id: str, key: str) -> Path:
    # Neither a posting key nor a profile name is interpreted as a filesystem path.
    identifier = hashlib.sha256(f"{profile_id}\0{key}".encode()).hexdigest()[:24]
    return stores["data_dir"] / "applications" / identifier


def manifest(directory: Path) -> dict:
    path = directory / "manifest.json"
    if not path.exists():
        return {"prepared": False, "message": "No application documents yet."}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("The application record is unreadable. Apply again.") from error
    if not isinstance(result, dict) or result.get("prepared") is not True:
        raise ValueError("The application record is incomplete. Apply again.")
    return result


def document_path(directory: Path, record: dict, name: str) -> Path:
    from venator.tailor.version import PREPARATION_REVISION

    if name not in FILES:
        raise ValueError("Choose an application document to download.")
    if record.get("prepared") is not True or record.get("preparation_revision") != PREPARATION_REVISION:
        raise ValueError("Apply again before opening this application.")
    version = record.get("version")
    if not isinstance(version, str) or not version.isalnum():
        raise ValueError("No prepared document version is available.")
    if name in {"letter.txt", "letter.pdf"} and not isinstance(record.get("letterText"), str):
        raise ValueError("No cover letter was prepared for this version.")
    path = directory / "versions" / version / name
    if any(item.is_symlink() for item in (path.parent.parent, path.parent, path)):
        raise ValueError("The document location changed. Apply again.")
    hashes = record.get("file_hashes")
    expected = hashes.get(name) if isinstance(hashes, dict) else None
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ValueError("That document is missing or unreadable. Apply again.") from error
    if not isinstance(expected, str) or hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("The document changed or is damaged. Apply again.")
    preview_key = {"resume.txt": "resumeText", "letter.txt": "letterText"}.get(name)
    if preview_key is not None:
        try:
            matches = content.decode("utf-8") == record.get(preview_key)
        except UnicodeError:
            matches = False
        if not matches:
            raise ValueError("The document and its preview disagree. Apply again.")
    return path


def _verify_answer_pins(record: dict, stores: dict, profile_id: str) -> None:
    from venator.answers.store import pinned_current
    if not pinned_current(stores["data_dir"] / "answers", profile_id,
                          [*record.get("answerPins", []), *record.get("statementPins", [])]):
        raise ValueError("A reviewed answer or statement changed. Apply again before Fill.")


def _verify_bundle(directory: Path, record: dict) -> Path:
    resume = document_path(directory, record, "resume.pdf")
    document_path(directory, record, "resume.txt")
    if isinstance(record.get("letterText"), str):
        document_path(directory, record, "letter.txt")
        document_path(directory, record, "letter.pdf")
    elif (resume.parent / "letter.txt").exists() or (resume.parent / "letter.pdf").exists():
        raise ValueError("An unexpected letter is present. Apply again.")
    return resume


def _verify_freshness(record: dict, posting: dict | str, profile) -> None:
    from venator.match.store import filters_version
    from venator.tailor.version import _input_version
    if not isinstance(posting, str):
        from venator.discover.store import posting_revision
    revision = posting if isinstance(posting, str) else posting_revision(posting)

    if (record.get("posting_version") != revision
            or record.get("profile_version") != filters_version(profile.constraints_path, profile.targeting_path)
            or record.get("input_version") != _input_version(profile, profile.resume)):
        raise ValueError("The job or your profile changed. Apply again before using this application.")


def _status_revision(stores: dict, key: str) -> str:
    """Read the indexed, materialized revision; never replay the Posting store."""
    database_path = stores["database_path"]
    if not database_path.is_file():
        raise ValueError("Rebuild the View before checking prepared documents.")
    # A Discover append may precede its View rebuild. Refuse to call old
    # documents fresh until the View has caught up with every Posting day file.
    view_time = database_path.stat().st_mtime_ns
    if any(path.stat().st_mtime_ns > view_time for path in stores["postings_dir"].glob("*.jsonl")):
        raise ValueError("The View is older than the Posting store. Rebuild the View.")
    try:
        with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as database:
            row = database.execute("SELECT revision FROM posting_revisions WHERE key = ?", (key,)).fetchone()
    except sqlite3.DatabaseError as error:
        raise ValueError("The View cannot check prepared documents. Rebuild the View.") from error
    if row is None:
        raise ValueError("This job is no longer in the local board.")
    if not isinstance(row[0], str):
        raise ValueError("The View needs a rebuild before checking prepared documents.")
    return row[0]


def perform(args: argparse.Namespace) -> dict:
    profile = profile_from_arguments(args)
    stores = resolve_store_paths(data_dir=None, postings_dir=None, decisions_dir=None,
                                 track_dir=None, database_path=None)
    if args.action in {"answers", "answer", "retract", "replace", "promote"}:
        from venator.answers.store import append, latest, rows
        directory = stores["data_dir"] / "answers"
        if args.action == "answers":
            return {"answers": list(latest(directory, profile.identifier).values())}
        payload = json.loads(args.answer)
        if not isinstance(payload, dict):
            raise ValueError("Choose an answer.")
        if args.action in {"retract", "replace", "promote"}:
            old = next((row for row in rows(directory, profile.identifier) if row["id"] == payload.get("id")), None)
            if old is None:
                raise ValueError("This answer is no longer in the library.")
            if latest(directory, profile.identifier).get((old["question"], old["scope"])) != old:
                raise ValueError("This answer changed. Reload the Answers view.")
            from venator.answers.store import promote, replace
            if args.action == "promote":
                return promote(directory, profile.identifier, old)
            return replace(directory, profile.identifier, old, payload.get("answer") if args.action == "replace" else None)
        kind = payload.get("kind")
        posting = None
        if kind != "statement":
            from venator.discover.store import load_postings
            posting = next((row for row in load_postings(stores["postings_dir"]) if row["key"] == args.key), None)
            if posting is None:
                raise ValueError("Choose a Posting for this answer.")
        if set(payload) != {"text", "kind", "answer", "universal", "eeo"} or not isinstance(payload["universal"], bool) or not isinstance(payload["eeo"], bool):
            raise ValueError("Choose a question, answer and employer scope.")
        return append(directory, profile.identifier, text=payload["text"], kind=kind,
                      answer=payload["answer"], board=str(posting.get("board") or "") if posting else "any",
                      company=str(posting.get("company") or "") if posting else "",
                      universal=payload["universal"], eeo=payload["eeo"])
    if args.action == "status":
        # The stamps are the ownership lock. Routine reads must not replay all
        # foreign rows in the Filter Decision store just to check a manifest.
        for name in ("decisions_dir", "track_dir"):
            owner = store_owner(stores[name])
            if owner is not None and owner != profile.identifier:
                raise ValueError(f"The {name} store belongs to another Profile: {owner!r}.")
        directory = application_directory(stores, profile.identifier, args.key)
        from venator.browser.handoff import _read_json, handoff_state_path
        handoff = _read_json(handoff_state_path(directory)) or {}
        marker = handoff.get("marker")
        parts = args.key.split(":")
        confirmed = (handoff.get("state") == "applied" and isinstance(marker, dict)
                     and len(parts) == 3 and parts[0] == "greenhouse"
                     and marker.get("system") == parts[0] and marker.get("board") == parts[1]
                     and marker.get("job_id") == parts[2])
        if confirmed:
            from venator.track.record import record_event
            from venator.track.store import load_events
            from venator.view.build import build_database
            if not any(row.get("posting_key") == args.key and row.get("event") == "submit"
                       for row in load_events(stores["track_dir"])):
                record_event("submit", args.key, "employer confirmation: greenhouse",
                             identifier=profile.identifier, postings_dir=stores["postings_dir"],
                             decisions_dir=stores["decisions_dir"], track_dir=stores["track_dir"])
                build_database(profile=profile)
        session = {"handoff": handoff.get("state") if handoff.get("state") in {"ready", "closed", "applied"} else None,
                   "applied": confirmed, "candidates": handoff.get("candidates", []) if handoff.get("state") in {"ready", "closed", "applied"} else []}
        try:
            record = manifest(directory)
            if record.get("prepared") is True:
                revision = _status_revision(stores, args.key)
                _verify_freshness(record, revision, profile)
                _verify_answer_pins(record, stores, profile.identifier)
                _verify_bundle(directory, record)
            return {**record, **session}
        except ValueError as error:
            return {"prepared": False, "message": str(error), **session}
    from venator.discover.store import posting_revision
    from venator.match.store import filters_version, load_latest_decisions, load_postings, verify_decisions_dir
    from venator.track.record import record_event
    from venator.track.store import verify_track_dir
    from venator.view.build import build_database

    verify_decisions_dir(stores["decisions_dir"], profile.identifier)
    verify_track_dir(stores["track_dir"], profile.identifier)
    posting = next((p for p in load_postings(stores["postings_dir"]) if p["key"] == args.key), None)
    if posting is None:
        raise ValueError("This job is no longer in the local board.")
    directory = application_directory(stores, profile.identifier, args.key)
    if args.action == "file":
        record = manifest(directory)
        _verify_freshness(record, posting, profile)
        path = document_path(directory, record, args.file)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != record["file_hashes"][args.file]:
            raise ValueError("The document changed during download. Apply again before retrying.")
        return {"base64": base64.b64encode(content).decode("ascii")}

    events = {"save": "approve", "dismiss": "reject", "restore": "restore", "applied": "submit"}
    if args.action in events:
        detail = "Manually marked applied by the user." if args.action == "applied" else None
        record_event(events[args.action], args.key, detail, identifier=profile.identifier,
                     postings_dir=stores["postings_dir"], decisions_dir=stores["decisions_dir"], track_dir=stores["track_dir"])
        build_database(profile=profile)
        return {"message": {"save": "Job saved.", "dismiss": "Job dismissed. You can undo this.",
                            "restore": "Job restored.", "applied": "Application recorded as applied."}[args.action]}

    if args.action in {"edit", "fill"}:
        record = manifest(directory)
        _verify_freshness(record, posting, profile)
        _verify_answer_pins(record, stores, profile.identifier)
        resume_file = _verify_bundle(directory, record)
        if args.action == "edit":
            if args.version != record.get("version"):
                raise ValueError("This version changed. Review the current documents before editing.")
            from venator.tailor.prepare import edit_application
            edits = json.loads(args.edits)
            if not isinstance(edits, list) or any(not isinstance(row, dict) or set(row) != {"draft_id", "text"}
                                                   for row in edits):
                raise ValueError("Choose reviewed passages to edit.")
            return edit_application(directory, record, edits, posting, profile)
        from venator.browser.handoff import start_handoff
        return start_handoff(posting, profile, resume_file, directory,
                             letter_file=document_path(directory, record, "letter.pdf")
                             if isinstance(record.get("letterText"), str) else None,
                             questions=record.get("formQuestions", []))

    from venator.discover.refresh import refresh_posting
    from venator.discover.store import append_observations

    identity = (posting.get("key"), posting.get("source"), posting.get("board"))
    profile_version = filters_version(profile.constraints_path, profile.targeting_path)
    refreshed = refresh_posting(posting)
    if not isinstance(refreshed, Mapping) or tuple(refreshed.get(key) for key in ("key", "source", "board")) != identity:
        raise ValueError("The source returned a different job. No application action was taken.")
    append_observations(stores["postings_dir"], [refreshed])
    if refreshed.get("listing_status") == "closed":
        build_database(profile=profile)
        raise ValueError("This employer listing is closed. The board has been updated.")
    description = refreshed.get("description_html")
    if (refreshed.get("verification_status") != "verified"
            or refreshed.get("listing_status") != "open"
            or refreshed.get("description_kind") != "full"
            or not isinstance(description, str) or not description.strip()):
        build_database(profile=profile)
        raise ValueError("The full employer listing could not be verified. Open the source and try again when it is available.")
    if filters_version(profile.constraints_path, profile.targeting_path) != profile_version:
        raise ValueError("Your profile changed during verification. Retry with the updated profile.")
    from venator.match.run import run as filter_jobs
    filter_jobs(profile=profile)
    # Persist refreshed facts even if the selected provider or browser fails.
    build_database(profile=profile)
    decision = load_latest_decisions(stores["decisions_dir"]).get((args.key, "hard_filter"))
    if (decision is None or decision.get("posting_version") != posting_revision(refreshed)
            or decision.get("filters_version") != profile_version):
        raise ValueError("The refreshed job has no current eligibility check. Refresh the board and retry.")
    if decision.get("verdict") == "kill":
        raise ValueError(f"The refreshed job conflicts with your profile: {decision.get('reason', 'eligibility conflict')}")

    if args.action == "apply":
        from venator.answers.store import latest
        from venator.apply.form import greenhouse_questions, review_questions
        from venator.tailor.prepare import prepare_application
        question_rows: list[dict] = []
        pins: list[dict] = []
        read_form = False
        if refreshed.get("source") == "greenhouse":
            try:
                questions = greenhouse_questions(refreshed)
                question_rows, pins = review_questions(refreshed, profile, stores["data_dir"] / "answers", questions)
                read_form = True
            except (ValueError, OSError, TimeoutError):
                pass
        library = latest(stores["data_dir"] / "answers", profile.identifier)
        statements = [row for row in library.values() if row.get("kind") == "statement" and row.get("answer")]
        needs_letter = not read_form or any(row["kind"] == "file" and "letter" in row["label"].casefold()
                                            for row in question_rows)
        result = prepare_application(refreshed, profile, directory, provider="claude", cover_letter=needs_letter,
                                     form_questions=question_rows, answer_pins=pins, statements=statements)
        _verify_freshness(result, refreshed, profile)
        _verify_bundle(directory, result)
        record_event("prepare", args.key, "Prepared application documents.", identifier=profile.identifier,
                     postings_dir=stores["postings_dir"], decisions_dir=stores["decisions_dir"], track_dir=stores["track_dir"])
        build_database(profile=profile)
        return result
    raise ValueError("Choose an application action.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("key")
    parser.add_argument("--edits", default="[]")
    parser.add_argument("--answer", default="{}")
    parser.add_argument("--version")
    parser.add_argument("--file", choices=sorted(FILES))
    add_profile_argument(parser)
    args = parser.parse_args()
    try:
        with redirect_stdout(sys.stderr):
            if args.action == "file" and args.file is None:
                raise ValueError("Choose an application document to download.")
            result = perform(args)
    except Exception as error:
        print(json.dumps({"error": scrub(str(error))[:1200]}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
