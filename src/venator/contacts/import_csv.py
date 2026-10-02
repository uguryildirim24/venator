"""Import a private contacts CSV into this Install; never copy the source file."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from venator.contacts.store import append_contacts
from venator.discover.register import board_from_url
from venator.paths import install_stores
from venator.profile import add_profile_argument, announce_profile, profile_from_arguments

CSV_FIELDS = ("company", "person", "title", "conversation_angle", "contact_route", "sources")


def _company(value: str | None) -> str:
    return (value or "").strip().casefold()


def _triage_boards(path: Path) -> dict[str, list[str]]:
    """Use direct ATS links only: import never fetches or probes a board."""
    boards: dict[str, list[str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not {"company", "board_url_to_register"} <= set(reader.fieldnames or ()):
            raise ValueError("Triage CSV needs company and board_url_to_register columns.")
        for value in reader:
            company = _company(value["company"])
            for _, token in board_from_url((value["board_url_to_register"] or "").strip()):
                tokens = boards.setdefault(company, [])
                if token not in tokens:
                    tokens.append(token)
    return boards


def import_csv(path: Path, directory: Path, profile_id: str, *, triage: Path | None = None) -> dict[str, int]:
    """Read contacts, optionally joining company to the triage board links."""
    board_by_company = _triage_boards(triage) if triage is not None else {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not set(CSV_FIELDS) <= set(reader.fieldnames or ()):
            raise ValueError("Contacts CSV needs company, person, title, conversation_angle, contact_route, sources columns.")
        contacts = []
        for value in reader:
            contact = {field: value[field] for field in CSV_FIELDS}
            board = value.get("board", "")
            tokens = [board] if board else board_by_company.get(_company(value["company"]), [""])
            contacts.extend({**contact, "board": token} for token in tokens)
    return append_contacts(directory, profile_id, contacts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument("--csv", required=True, type=Path, help="private contacts CSV to read in place")
    parser.add_argument("--triage", type=Path, help="private triage CSV with company and board_url_to_register")
    args = parser.parse_args()
    try:
        profile = profile_from_arguments(args)
        stores = install_stores()
        announce_profile(profile)
        stores.announce()
        result = import_csv(args.csv, stores.contacts_dir, profile.identifier, triage=args.triage)
    except (ValueError, OSError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
