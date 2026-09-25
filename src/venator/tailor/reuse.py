"""Reuse verified writing for a layout-only change, without a provider call."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from venator.tailor.draft import CheckedDraft, _review


def reuse_checked_draft(record: Mapping, sources: Sequence[dict], *, cover_letter: bool) -> CheckedDraft | None:
    """The caller first checks the candidate/job version and artifact hashes."""
    provenance = record.get("draftProvenance")
    review = record.get("factualityReview")
    if not isinstance(provenance, list) or not isinstance(review, Mapping):
        return None
    if review.get("provider") != record.get("provider"):
        return None
    try:
        checks = _review({"checks": review.get("checks")}, provenance)
        verdicts = {row["draft_id"]: row["status"] for row in checks}
        originals = {
            row["entry_id"]: json.dumps(row["entry"], ensure_ascii=False, sort_keys=True, default=str)
            for row in sources
        }
        bullets = {bullet["id"]: bullet["text"] for row in sources for bullet in row["bullets"]}
        originals.update(bullets)
        rewritten, paragraphs = [], []
        for row in provenance:
            supported = verdicts[row["draft_id"]] == "supported"
            if row["kind"] == "resume_bullet":
                if len(row["source_ids"]) != 1:
                    return None
                identifier = row["source_ids"][0]
                if identifier not in bullets or row["original"] != bullets[identifier]:
                    return None
                final = row["draft"] if supported else row["original"]
                if row.get("final") != final or final not in record.get("resumeText", ""):
                    return None
                rewritten.append({"source_id": identifier, "text": final})
            elif row["kind"] == "letter_paragraph":
                expected = [{"source_id": identifier, "text": originals[identifier]} for identifier in row["source_ids"]]
                if row["original"] != expected or row.get("final") != (row["draft"] if supported else None):
                    return None
                if supported and cover_letter:
                    if row["final"] not in record.get("letterText", ""):
                        return None
                    paragraphs.append({"source_ids": row["source_ids"], "text": row["final"]})
            else:
                return None
        if cover_letter and not any(row["source_ids"] for row in paragraphs):
            return None
        if len({row["source_id"] for row in rewritten}) != len(rewritten):
            return None
        selection = record.get("draftSelection")
        if selection is not None:
            if not isinstance(selection, Mapping) or selection.get("resume_bullets") != rewritten:
                return None
            if cover_letter and selection.get("letter_paragraphs") != paragraphs:
                return None
        elif not provenance:
            # Older bundles without a selection snapshot need actual passage
            # provenance; an empty review cannot establish what was checked.
            return None
        return CheckedDraft(tuple(row["entry_id"] for row in sources), tuple(rewritten), tuple(paragraphs), tuple(provenance), tuple(checks))
    except (KeyError, TypeError, ValueError):
        return None
