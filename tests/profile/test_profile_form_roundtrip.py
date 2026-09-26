"""What the Profile screen writes, read by the loader that decides what a Profile means.

``ui/server/onboarding/profile-form.ts`` shows an existing Profile as fields and
writes a form's differences back into the same three files. It reads YAML to do
that, with a YAML 1.1 parser in TypeScript — so this file exists to say, in the
interpreter that matters, that what it writes is what PyYAML reads:

* a word YAML 1.1 resolves to something other than text (``no``, ``12:30``) comes
  back as the text that was typed;
* a form nobody changed writes nothing at all;
* everything the form does not model — the Jev policy, an entry's ``primary``
  flag, the wording patterns, ``option_aliases`` — is still there afterwards;
* and the result loads through ``venator.profile.load_profile`` and hashes through
  ``filters_version``, which are the two claims only Python can make.

The reader and patcher are spawned rather than re-implemented, over the same
one-JSON-document-in, one-out seam ``test_dashboard_verify_adapter.py`` uses:
``ui/tests/patch-profile.ts``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from venator.match.store import filters_version
from venator.profile import load_profile

REPOSITORY = Path(__file__).resolve().parents[2]
SEAM = REPOSITORY / "ui" / "tests" / "patch-profile.ts"
EXAMPLE = REPOSITORY / "profiles" / "example"
FILES = ("resume.yaml", "constraints.yaml", "targeting.yaml")


def _drive(script: Path, request: str) -> object:
    """One process, one JSON document in, one out — ``test_dashboard_verify_adapter.py``'s seam."""
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is needed to run the Profile screen's reader and patcher; nothing here re-implements them")
    finished = subprocess.run(
        [node, str(script)],
        input=request,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=300,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


def _documents(directory: Path) -> dict[str, str]:
    return {name: (directory / name).read_text(encoding="utf-8") for name in FILES}


def _form_of(documents: dict[str, str]) -> dict:
    answer = _drive(SEAM, json.dumps({"documents": documents}))
    assert isinstance(answer, dict) and "form" in answer, answer
    return answer["form"]


def _patched(original: dict[str, str], form: dict) -> dict[str, str]:
    answer = _drive(SEAM, json.dumps({"original": original, "form": form}))
    assert isinstance(answer, dict) and "documents" in answer, answer
    return answer["documents"]


def _write(directory: Path, documents: dict[str, str]) -> None:
    for name in FILES:
        (directory / name).write_text(documents[name], encoding="utf-8")


def test_a_form_nobody_changed_writes_nothing() -> None:
    original = _documents(EXAMPLE)

    assert _patched(original, _form_of(original)) == original


def test_what_the_screen_writes_is_what_the_loader_reads(tmp_path: Path) -> None:
    original = _documents(EXAMPLE)
    before = {name: yaml.safe_load(text) for name, text in original.items()}
    form = _form_of(original)

    form["resume"]["name"] = "no"
    form["resume"]["contact"]["email"] = "avery@example.org"
    form["resume"]["education"][0]["gpa"] = "3.9/4.0"
    form["resume"]["experience"].append(
        {
            "origin": None,
            "org": "New Co",
            "role": "Engineer",
            "location": "",
            "dates": "2024 - 2025",
            "bullets": ["Did: things", "- dashy", "Line one\nline two"],
        }
    )
    form["constraints"]["work_authorization"] = {
        "status": "Citizen",
        "requires_sponsorship": "no",
        "authorized_to_work": "yes",
    }
    form["constraints"]["screening"]["how_heard"] = "12:30"
    form["constraints"]["screening"]["country"] = "yes"
    form["targeting"]["search"] = {"queries": ["backend engineer", "sre"], "locations": [], "remote": None}
    form["targeting"]["employers"] = [
        {"source": "greenhouse", "board": "cloudflare", "name": "Cloudflare"},
        {"source": "ashby", "board": "linear", "name": "Linear"},
        {"source": "workday", "board": "yes", "name": "Yes Corp"},
    ]
    form["targeting"]["filters"]["enabled"] = ["education_fit", "role_target", "work_authorization"]
    form["targeting"]["filters"]["role_target"]["accept"] = ["senior"]
    form["targeting"]["filters"]["role_target"]["exclude_terms"] = ["sales"]
    form["targeting"]["filters"]["education_fit"].update({"holds": "master", "in_progress": "doctorate", "experience_years_kill": None})

    patched = _patched(original, form)
    _write(tmp_path, patched)
    after = {name: yaml.safe_load(text) for name, text in patched.items()}

    resume, constraints, targeting = after["resume.yaml"], after["constraints.yaml"], after["targeting.yaml"]
    # What was typed is what is read, whatever YAML 1.1 would have made of it bare.
    assert resume["name"] == "no"
    assert resume["contact"]["email"] == "avery@example.org"
    assert resume["education"][0]["gpa"] == "3.9/4.0"
    assert resume["experience"][1] == {
        "org": "New Co",
        "role": "Engineer",
        "dates": "2024 - 2025",
        "bullets": ["Did: things", "- dashy", "Line one\nline two"],
    }
    assert constraints["work_authorization"] == {"status": "Citizen", "requires_sponsorship": False, "authorized_to_work": True}
    assert constraints["screening"]["how_heard"] == "12:30"
    assert constraints["screening"]["country"] == "yes"
    assert targeting["search"]["remote"] is None
    assert targeting["sources"]["names"] == {"cloudflare": "Cloudflare", "linear": "Linear", "yes": "Yes Corp"}
    assert targeting["sources"]["boards"] == {"greenhouse": ["cloudflare"], "lever": [], "ashby": ["linear"], "workday": ["yes"]}
    assert targeting["filters"]["role_target"]["accept"] == ["senior"]
    assert targeting["filters"]["role_target"]["exclude"]["terms"] == ["sales"]
    assert targeting["filters"]["education_fit"]["experience_years_kill"] is None

    # What the form does not show is exactly as it was.
    assert resume["education"][0]["primary"] is True
    assert resume["memberships"] == before["resume.yaml"]["memberships"]
    assert constraints["option_aliases"] == before["constraints.yaml"]["option_aliases"]
    assert constraints["volume"] == before["constraints.yaml"]["volume"]
    assert constraints["screening"]["eeo"] == before["constraints.yaml"]["screening"]["eeo"]
    assert targeting["profile"] == before["targeting.yaml"]["profile"]
    assert targeting["filters"]["jev"] == before["targeting.yaml"]["filters"]["jev"]
    assert targeting["filters"]["qualification_mode"] == "jev"
    assert targeting["filters"]["work_authorization"] == before["targeting.yaml"]["filters"]["work_authorization"]
    assert targeting["filters"]["role_target"]["levels"] == before["targeting.yaml"]["filters"]["role_target"]["levels"]
    assert targeting["search"]["seniority"] == before["targeting.yaml"]["search"]["seniority"]
    assert "# Fictional evidence for testing only" in patched["resume.yaml"]
    assert "# scaffold: true marks this a template." in patched["targeting.yaml"]

    # The two claims only this interpreter can make.
    profile = load_profile(tmp_path)
    assert profile.filters.enabled == ("education_fit", "role_target", "work_authorization")
    assert profile.filters.education_fit.holds == "master"
    assert profile.filters.education_fit.in_progress == "doctorate"
    assert profile.filters.education_fit.experience_years_kill is None
    assert profile.filters.role_target.accept == ("senior",)
    assert profile.sources.names["yes"] == "Yes Corp"
    assert profile.filters.jev is not None
    assert profile.filters.jev.policy_version == "example-shadow-1"
    assert filters_version(tmp_path / "targeting.yaml", tmp_path / "constraints.yaml") != filters_version(
        EXAMPLE / "targeting.yaml", EXAMPLE / "constraints.yaml"
    )


def test_the_screen_refuses_what_the_loader_would(tmp_path: Path) -> None:
    """The form's own check is the loader's rule, said before the load-back has to."""
    original = _documents(EXAMPLE)
    form = _form_of(original)
    form["targeting"]["filters"]["education_fit"]["experience_years_kill"] = 40

    patched = _patched(original, form)
    _write(tmp_path, patched)
    with pytest.raises(Exception, match="experience_years_implausible"):
        load_profile(tmp_path)


def test_the_seam_answers_a_refusal_as_a_value() -> None:
    original = _documents(EXAMPLE)
    form = _form_of(original)
    form["resume"]["education"][0]["origin"] = 7

    answer = _drive(SEAM, json.dumps({"original": original, "form": form}))

    assert answer == {"refused": "bad_request", "field": "resume.education", "message": answer["message"]}
