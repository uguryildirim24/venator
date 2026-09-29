from __future__ import annotations

from pathlib import Path

from venator.score.store import Score, append_score, read_scores


def test_current_score_requires_input_and_model_and_append_preserves_history(tmp_path: Path) -> None:
    first = Score('posting', 'a' * 64, 'adapter-one', 0.7, '2026-09-29T00:00:00+00:00')
    append_score(tmp_path, first)
    original = (tmp_path / '2026-09-29.jsonl').read_bytes()
    second = Score('posting', 'b' * 64, 'adapter-two', 0.2, '2026-09-29T01:00:00+00:00')
    append_score(tmp_path, second)
    assert (tmp_path / '2026-09-29.jsonl').read_bytes().startswith(original)
    scores = read_scores(tmp_path)
    assert scores[('posting', 'a' * 64, 'adapter-one')] == first
    assert scores[('posting', 'b' * 64, 'adapter-two')] == second
    assert ('posting', 'b' * 64, 'adapter-one') not in scores
    assert ('posting', 'a' * 64, 'adapter-two') not in scores
