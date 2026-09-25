"""Local application actions used by the desktop app. Never submits a form."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from collections.abc import Mapping
from contextlib import redirect_stdout
from pathlib import Path

from venator.discover.store import posting_revision
from venator.match.store import filters_version, load_latest_decisions, load_postings, verify_decisions_dir
from venator.paths import resolve_store_paths
from venator.profile import add_profile_argument, profile_from_arguments
from venator.secrets import scrub
from venator.track.record import record_event
from venator.track.store import verify_track_dir
from venator.view.build import build_database

FILES = frozenset({"resume.pdf", "resume.txt", "letter.txt"})
ACTIONS = ("status", "file", "prepare", "save", "dismiss", "restore", "applied", "handoff")


def application_directory(stores: dict, profile_id: str, key: str) -> Path:
    # Neither a posting key nor a profile name is interpreted as a filesystem path.
    identifier = hashlib.sha256(f"{profile_id}\0{key}".encode()).hexdigest()[:24]
    return stores["data_dir"] / "applications" / identifier


def manifest(directory: Path) -> dict:
    path = directory / "manifest.json"
    if not path.exists():
        return {"prepared": False, "message": "No documents prepared yet."}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("The application record is unreadable. Prepare the documents again.") from error
    if not isinstance(result, dict) or result.get("prepared") is not True:
        raise ValueError("The application record is incomplete. Prepare the documents again.")
    return result


def document_path(directory: Path, record: dict, name: str) -> Path:
    from venator.tailor.prepare import PREPARATION_REVISION

    if name not in FILES:
        raise ValueError("Choose an application document to download.")
    if record.get("prepared") is not True or record.get("preparation_revision") != PREPARATION_REVISION:
        raise ValueError("Prepare fresh documents before opening this application.")
    version = record.get("version")
    if not isinstance(version, str) or not version.isalnum():
        raise ValueError("No prepared document version is available.")
    if name == "letter.txt" and not isinstance(record.get("letterText"), str):
        raise ValueError("No cover letter was prepared for this version.")
    path = directory / "versions" / version / name
    if any(item.is_symlink() for item in (path.parent.parent, path.parent, path)):
        raise ValueError("The prepared document location changed. Prepare the documents again.")
    hashes = record.get("file_hashes")
    expected = hashes.get(name) if isinstance(hashes, dict) else None
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ValueError("That document is missing or unreadable. Prepare the documents again.") from error
    if not isinstance(expected, str) or hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("The prepared document changed or is damaged. Prepare the documents again.")
    preview_key = {"resume.txt": "resumeText", "letter.txt": "letterText"}.get(name)
    if preview_key is not None:
        try:
            matches = content.decode("utf-8") == record.get(preview_key)
        except UnicodeError:
            matches = False
        if not matches:
            raise ValueError("The document and its preview disagree. Prepare the documents again.")
    return path


def _verify_bundle(directory: Path, record: dict) -> Path:
    resume = document_path(directory, record, "resume.pdf")
    document_path(directory, record, "resume.txt")
    if isinstance(record.get("letterText"), str):
        document_path(directory, record, "letter.txt")
    elif (resume.parent / "letter.txt").exists():
        raise ValueError("An unexpected letter is present. Prepare the documents again.")
    return resume


def _verify_freshness(record: dict, posting: dict, profile) -> None:
    from venator.tailor.prepare import _input_version

    if (record.get("posting_version") != posting_revision(posting)
            or record.get("profile_version") != filters_version(profile.constraints_path, profile.targeting_path)
            or record.get("input_version") != _input_version(profile, profile.resume)):
        raise ValueError("The job or your profile changed. Prepare fresh documents before using this application.")


def perform(args: argparse.Namespace) -> dict:
    profile = profile_from_arguments(args)
    stores = resolve_store_paths(data_dir=None, postings_dir=None, decisions_dir=None, track_dir=None)
    verify_decisions_dir(stores["decisions_dir"], profile.identifier)
    verify_track_dir(stores["track_dir"], profile.identifier)
    posting = next((p for p in load_postings(stores["postings_dir"]) if p["key"] == args.key), None)
    if posting is None:
        raise ValueError("This job is no longer in the local board.")
    directory = application_directory(stores, profile.identifier, args.key)
    if args.action == "status":
        try:
            record = manifest(directory)
            if record.get("prepared") is True:
                _verify_freshness(record, posting, profile)
                _verify_bundle(directory, record)
            return record
        except ValueError as error:
            return {"prepared": False, "message": str(error)}
    if args.action == "file":
        record = manifest(directory)
        _verify_freshness(record, posting, profile)
        path = document_path(directory, record, args.file)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != record["file_hashes"][args.file]:
            raise ValueError("The document changed during download. Retry after preparing it again.")
        return {"base64": base64.b64encode(content).decode("ascii")}

    events = {"save": "approve", "dismiss": "reject", "restore": "restore", "applied": "submit"}
    if args.action in events:
        detail = "Manually marked applied by the user." if args.action == "applied" else None
        record_event(events[args.action], args.key, detail, identifier=profile.identifier,
                     postings_dir=stores["postings_dir"], decisions_dir=stores["decisions_dir"], track_dir=stores["track_dir"])
        build_database(profile=profile)
        return {"message": {"save": "Job saved.", "dismiss": "Job dismissed. You can undo this.",
                            "restore": "Job restored.", "applied": "Application recorded as applied."}[args.action]}

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

    if args.action == "prepare":
        from venator.tailor.prepare import prepare_application
        result = prepare_application(refreshed, profile, directory, provider=args.provider, cover_letter=args.cover_letter)
        _verify_freshness(result, refreshed, profile)
        _verify_bundle(directory, result)
        record_event("prepare", args.key, "Prepared application documents.", identifier=profile.identifier,
                     postings_dir=stores["postings_dir"], decisions_dir=stores["decisions_dir"], track_dir=stores["track_dir"])
        build_database(profile=profile)
        return result
    record = manifest(directory)
    _verify_freshness(record, refreshed, profile)
    resume_file = _verify_bundle(directory, record)
    from venator.browser.handoff import start_handoff
    return start_handoff(refreshed, profile, resume_file, directory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("key")
    parser.add_argument("--provider", choices=("claude", "codex", "api"))
    parser.add_argument("--cover-letter", action="store_true")
    parser.add_argument("--file", choices=sorted(FILES))
    add_profile_argument(parser)
    args = parser.parse_args()
    try:
        with redirect_stdout(sys.stderr):
            if args.action == "prepare" and args.provider is None:
                raise ValueError("Choose a provider for preparation.")
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
