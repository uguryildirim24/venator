from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from venator.contacts.import_csv import import_csv
from venator.contacts.store import append_contacts, rows
from venator.profile.claim import store_owner

CONTACT = {"company": "Example Lab", "person": "Alex Example", "title": "Research lead",
           "conversation_angle": "Ask about assay development", "contact_route": "https://example.com/alex",
           "sources": "https://example.com/team", "board": "examplelab"}


def write_csv(path: Path, contacts: list[dict], *, board: bool = True) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        fields = ("company", "person", "title", "conversation_angle", "contact_route", "sources")
        writer = csv.DictWriter(output, fieldnames=[*fields, *(["board"] if board else [])])
        writer.writeheader()
        for contact in contacts:
            value = {field: contact[field] for field in fields}
            if board:
                value["board"] = contact["board"]
            writer.writerow(value)


def test_import_multiple_contacts_exact_duplicates_and_append_only(tmp_path: Path) -> None:
    source, directory = tmp_path / "contacts.csv", tmp_path / "data/contacts"
    second = {**CONTACT, "person": "Jamie Example", "conversation_angle": "Assays, and\nnew instruments"}
    write_csv(source, [CONTACT, CONTACT, second])
    original_source = source.read_bytes()
    assert import_csv(source, directory, "profile-a") == {"rows": 3, "imported": 2, "duplicates": 1}
    assert store_owner(directory) == "profile-a"
    stored = rows(directory, "profile-a")
    assert [row["person"] for row in stored] == ["Alex Example", "Jamie Example"]
    assert stored[1]["conversation_angle"] == second["conversation_angle"]
    assert all(row["id"] and row["at"] and row["profile_id"] == "profile-a" for row in stored)
    day_file = next(directory.glob("*.jsonl"))
    before = day_file.read_bytes()
    assert import_csv(source, directory, "profile-a") == {"rows": 3, "imported": 0, "duplicates": 3}
    assert day_file.read_bytes() == before
    changed = {**CONTACT, "title": "Science director"}
    assert append_contacts(directory, "profile-a", [changed])["imported"] == 1
    assert day_file.read_bytes().startswith(before)
    assert source.read_bytes() == original_source


def test_six_column_csv_and_read_never_claims(tmp_path: Path) -> None:
    directory, source = tmp_path / "contacts", tmp_path / "contacts.csv"
    assert rows(directory, "profile-a") == []
    assert not directory.exists()
    write_csv(source, [CONTACT], board=False)
    assert import_csv(source, directory, "profile-a")["imported"] == 1
    assert rows(directory, "profile-a")[0]["board"] == ""


def write_triage(path: Path, entries: list[tuple[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=("number", "company", "location", "class", "hiring_system",
                                                   "board_url_to_register", "open_postings", "notes"))
        writer.writeheader()
        for number, (company, url) in enumerate(entries, start=1):
            writer.writerow({"number": number, "company": company, "board_url_to_register": url,
                             "notes": "https://jobs.lever.co/not-the-board"})


@pytest.mark.parametrize(("url", "token"), [
    ("https://job-boards.greenhouse.io/examplelab", "examplelab"),
    ("https://jobs.lever.co/examplelab", "examplelab"),
    ("https://jobs.ashbyhq.com/examplelab", "examplelab"),
    ("https://careers.smartrecruiters.com/ExampleLab", "ExampleLab"),
    ("https://examplelab.wd1.myworkdayjobs.com/en-US/External", "examplelab.wd1~External"),
])
def test_exact_triage_header_joins_by_company_and_reads_only_board_link(tmp_path: Path, url: str, token: str) -> None:
    source, triage, directory = tmp_path / "contacts.csv", tmp_path / "triage.csv", tmp_path / "contacts"
    write_csv(source, [CONTACT, {**CONTACT, "person": "Jamie Example"}], board=False)
    write_triage(triage, [("Unrelated Lab", "https://jobs.lever.co/unrelated"), ("  EXAMPLE LAB ", url)])
    original = triage.read_bytes()
    assert import_csv(source, directory, "profile-a", triage=triage) == {"rows": 2, "imported": 2, "duplicates": 0}
    assert [row["board"] for row in rows(directory, "profile-a")] == [token, token]
    assert import_csv(source, directory, "profile-a", triage=triage)["duplicates"] == 2
    assert triage.read_bytes() == original


def test_explicit_board_wins_and_missing_or_non_ats_links_keep_company_contacts(tmp_path: Path) -> None:
    source, triage, directory = tmp_path / "contacts.csv", tmp_path / "triage.csv", tmp_path / "contacts"
    write_csv(source, [CONTACT, {**CONTACT, "company": "Plain Lab", "board": ""},
                       {**CONTACT, "company": "Missing Lab", "board": ""}])
    write_triage(triage, [(CONTACT["company"], "https://jobs.ashbyhq.com/from-triage"),
                         ("Plain Lab", "https://example.com/careers")])
    import_csv(source, directory, "profile-a", triage=triage)
    assert [row["board"] for row in rows(directory, "profile-a")] == [CONTACT["board"], "", ""]


def test_foreign_profile_refused_without_changes(tmp_path: Path) -> None:
    directory = tmp_path / "contacts"
    append_contacts(directory, "profile-a", [CONTACT])
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(ValueError, match="another Profile"):
        rows(directory, "profile-b")
    with pytest.raises(ValueError, match="another Profile"):
        append_contacts(directory, "profile-b", [CONTACT])
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    (directory / ".profile").unlink()
    with pytest.raises(ValueError, match="another Profile"):
        append_contacts(directory, "profile-b", [CONTACT])
    assert not (directory / ".profile").exists()


def test_bad_csv_does_not_partially_import(tmp_path: Path) -> None:
    source, directory = tmp_path / "contacts.csv", tmp_path / "contacts"
    source.write_text("company,person\nExample Lab,Alex Example\n", encoding="utf-8")
    with pytest.raises(ValueError, match="columns"):
        import_csv(source, directory, "profile-a")
    write_csv(source, [CONTACT, {**CONTACT, "person": ""}])
    with pytest.raises(ValueError, match="person"):
        import_csv(source, directory, "profile-a")
    assert not directory.exists()


def test_cli_writes_to_install_not_working_directory(tmp_path: Path) -> None:
    install, working = tmp_path / "install", tmp_path / "checkout"
    working.mkdir()
    profile = install / "profiles/synthetic"
    profile.mkdir(parents=True)
    (profile / "targeting.yaml").write_text("profile:\n  id: synthetic-profile-id\n", encoding="utf-8")
    source = tmp_path / "contacts.csv"
    write_csv(source, [CONTACT], board=False)
    triage = tmp_path / "triage.csv"
    write_triage(triage, [(CONTACT["company"], "https://jobs.ashbyhq.com/examplelab")])
    result = subprocess.run(
        [sys.executable, "-m", "venator.contacts.import_csv", "--csv", str(source), "--triage", str(triage),
         "--profile", "synthetic"],
        cwd=working, env={**os.environ, "VENATOR_HOME": str(install)}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"rows": 1, "imported": 1, "duplicates": 0}
    stored = rows(install / "data/contacts", "synthetic-profile-id")[0]
    assert stored["person"] == CONTACT["person"]
    assert stored["board"] == "examplelab"
    assert list(working.iterdir()) == []
