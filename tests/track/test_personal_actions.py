import json

from venator.track.record import record_event
from venator.track.store import fold_states, load_events, validate_event


def test_manual_application_and_undo_do_not_require_a_score_or_fill(tmp_path):
    postings = tmp_path / "postings"
    postings.mkdir()
    key = "lever:example:one"
    (postings / "jobs.jsonl").write_text(json.dumps({"key": key}) + "\n", encoding="utf-8")
    kwargs = {"postings_dir": postings, "decisions_dir": tmp_path / "decisions", "track_dir": tmp_path / "track"}
    event = record_event("submit", key, "User says they applied on the employer site.", **kwargs)
    assert event["actor"] == "owner"
    record_event("reject", key, **kwargs)
    record_event("restore", key, **kwargs)
    assert fold_states(load_events(kwargs["track_dir"]), {})[key]["state"] == "queued"
    assert len(load_events(kwargs["track_dir"])) == 3


def test_legacy_submission_records_remain_readable():
    validate_event({"posting_key": "test:one", "event": "submit", "actor": "pipeline", "detail": "Previous confirmation", "at": "2026-09-01T12:00:00Z"})
