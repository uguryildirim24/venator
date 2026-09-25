"""A copied Profile's on-demand Jev check is shadow and belongs to its own binding."""

from __future__ import annotations

import json
from pathlib import Path

from venator.llm.system_one import SystemOneHttpResult
from venator.profile.loader import load_profile
from venator.qualify.compile import compile_profile
from venator.qualify.jev import current_mode, execute_prepared_cases, select_work
from venator.qualify.jev_policy import prepare_jev_case
from venator.qualify.jev_release import JEV_RELEASE
from venator.qualify.store import qualification_rows
from venator.qualify.versions import jev_accepted_profile_hash, jev_policy_hash

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "jev"


def test_copied_example_jev_it_uses_its_own_shadow_binding(tmp_path: Path) -> None:
    source = ROOT / "profiles" / "example"
    profile_dir = tmp_path / "new-search"
    profile_dir.mkdir()
    targeting = (source / "targeting.yaml").read_text(encoding="utf-8")
    targeting = targeting.replace("name: example", "name: new-search", 1).replace("  scaffold: true\n", "", 1)
    (profile_dir / "targeting.yaml").write_text(targeting, encoding="utf-8")
    for name in ("resume.yaml", "constraints.yaml"):
        (profile_dir / name).write_bytes((source / name).read_bytes())
    profile = load_profile(profile_dir)
    assert profile.filters.qualification_mode == "jev"
    assert profile.filters.jev is not None
    posting = json.loads((FIXTURE / "posting.json").read_text(encoding="utf-8"))
    prepared = prepare_jev_case(posting, profile, "2026-09")
    assert prepared.case is not None
    assert prepared.case.bindings.accepted_profile_hash == jev_accepted_profile_hash(compile_profile(profile, as_of_month="2026-09").profile_hash, jev_policy_hash(profile.filters.jev))
    store = tmp_path / "qualifications"
    assert current_mode(profile, "2026-09", store) == "shadow"
    selected = select_work([posting], profile=profile, month="2026-09", mode="shadow",
                           qualifications_dir=store, named_keys=None, release=JEV_RELEASE,
                           decided_at="2026-09-18T00:00:00+00:00")
    assert len(selected.prepared) == 1
    raw = (FIXTURE / "valid_raw_response.json").read_bytes()

    def fake_client(request):
        assert request.headers["Authorization"] == "Bearer fake-test-key"
        return SystemOneHttpResult(200, raw)

    result = execute_prepared_cases(
        selected.prepared, qualifications_dir=store, profile_id=profile.identifier,
        mode="shadow", day="2026-09-18", decided_at="2026-09-18T00:00:00+00:00",
        offline=False, max_usd=1, max_requests=None, poster=fake_client,
        environ={"TYPESAFE_API_KEY": "fake-test-key"},
    )
    assert result.valid_responses == 1
    rows = qualification_rows(store)
    assert len(rows) == 1
    assert rows[0]["accepted_profile_hash"] == jev_accepted_profile_hash(compile_profile(profile, as_of_month="2026-09").profile_hash, jev_policy_hash(profile.filters.jev))
    assert rows[0]["mode"] == "shadow"
    assert rows[0]["decision"] in {"prioritize", "review", "exclude"}
    again = select_work([posting], profile=profile, month="2026-09", mode="shadow",
                        qualifications_dir=store, named_keys=None, release=JEV_RELEASE,
                        decided_at="2026-09-18T00:00:00+00:00")
    assert not again.prepared
    assert again.report.current_assessments == 1
    assert current_mode(profile, "2026-09", store) == "shadow"
