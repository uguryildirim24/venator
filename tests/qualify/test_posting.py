"""Canonical Posting and conservative requirement-span contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.qualify.posting import POSTING_CHAR_CAP, canonical_posting, free_atoms

FIXTURES = Path(__file__).parent / "fixtures" / "postings"


@pytest.mark.parametrize("name", ["split-modalities", "simple-or", "nested-or", "pursuing",
                                    "uncued", "negated", "free", "free-two", "zero-spans"])
def test_obligation_fixture_engine_golden(name: str) -> None:
    source = json.loads((FIXTURES / f"{name}.json").read_text())
    canonical = canonical_posting(source["posting"], {})
    actual = {
        "description_text": canonical.description_text,
        "spans": [{
            "id": span.id, "text": span.text, "start": span.start, "end": span.end,
            "hint": span.modality_hint, "atom_kinds": [atom.kind for atom in span.atoms],
            "atom_hints": [atom.modality_hint for atom in span.atoms],
            "tree": None if span.tree is None else str(span.tree),
        } for span in canonical.spans],
    }
    assert actual == source["engine_golden"]
    assert all(canonical.description_text[span.start:span.end] == span.text for span in canonical.spans)
    for index, obligation in enumerate(source["obligation_golden"], 1):
        assert obligation["obligation_id"] == f"ob-{index:03d}"
        quote = obligation["quote"]
        assert quote in canonical.description_text
        binding = obligation["atom_binding"]
        if binding == "free":
            assert not canonical.spans and len(free_atoms(quote)) == 1
        elif binding == "split_required":
            assert not canonical.spans and len(free_atoms(quote)) == 2
        elif binding:
            assert any(atom.atom_id == binding and quote in span.text and
                       atom.start < span.start + span.text.find(quote) + len(quote) and
                       atom.end > span.start + span.text.find(quote)
                       for span in canonical.spans for atom in span.atoms)


def test_text_normalization_and_identity() -> None:
    posting = {"key": "lever:fixture:1", "title": "Scientist", "description_html":
               "&lt;h2&gt;Requirements&lt;/h2&gt;&lt;p&gt;  BS&nbsp; required  &lt;/p&gt;"}
    first = canonical_posting(posting, {"posting_to_group": {"lever:fixture:1": "group-1"}})
    second = canonical_posting(posting, {"posting_to_group": {"lever:fixture:1": "group-1"}})
    assert first == second
    assert first.description_text == "Requirements:\nBS required"
    assert first.posting_group == "group-1"
    assert canonical_posting(posting, {}).posting_group == "lever:fixture:1"
    with pytest.raises(TypeError):
        canonical_posting(posting)  # type: ignore[call-arg]


def test_caps_and_snippet_refusal_signal() -> None:
    posting = {"key": "x", "description_html": "x" * POSTING_CHAR_CAP}
    assert not canonical_posting(posting, {}).too_long
    assert canonical_posting({**posting, "description_html": "x" * (POSTING_CHAR_CAP + 1)}, {}).too_long
    lines = "<h2>Requirements</h2>" + "".join(f"<p>Skill {i} required.</p>" for i in range(41))
    assert canonical_posting({"key": "x", "description_html": lines}, {}).too_many_spans
    assert canonical_posting({"key": "x", "description_kind": "snippet", "description_html": "BS required"}, {}).description_kind == "snippet"


def test_span_exclusions_and_duplicate_quote_offsets() -> None:
    posting = {"key": "x", "description_html": "<h2>Responsibilities</h2><p>Python required.</p>"
               "<p>Our company uses Python.</p><h2>Requirements</h2><p>Python required.</p>"}
    canonical = canonical_posting(posting, {})
    assert [span.text for span in canonical.spans] == ["Python required."]
    assert canonical.description_text[canonical.spans[0].start:canonical.spans[0].end] == "Python required."


def test_inline_note_does_not_end_responsibilities() -> None:
    html = ('<h2>Responsibilities</h2><p>Note:</p><p>The team needs you to build tools.</p>'
            '<h2>Qualifications</h2><p>BS required</p><h2>Benefits</h2><p>Free lunch.</p>')
    p = canonical_posting({'key': 'note', 'description_html': html}, {})
    assert [s.text for s in p.spans] == ['BS required']


def test_colon_content_is_not_a_section_heading() -> None:
    p = canonical_posting({'key': 'colon', 'description_html':
        '<h2>Qualifications</h2><p>Note:</p><p>Laboratory experience</p>'}, {})
    assert [s.text for s in p.spans] == ['Note:', 'Laboratory experience']


def test_atom_cap_independent_of_span_cap() -> None:
    for count, expected in ((100, False), (101, True)):
        html = '<h2>Requirements</h2>' + ''.join('<p>BS and MS and PhD and 2 years</p>' for _ in range(25))
        if count == 101:
            html += '<p>BS required</p>'
        p = canonical_posting({'key': 'atoms', 'description_html': html}, {})
        assert len(p.spans) < 40
        assert sum(len(s.atoms) for s in p.spans) == count
        assert p.too_many_spans is expected


def test_need_cue_and_employer_stack_exclusion() -> None:
    p = canonical_posting({'key': 'stack', 'description_html':
        '<h2>Qualifications</h2><p>Our platform requires Python.</p>'
        '<p>The team needs you to build tools.</p>'}, {})
    assert [s.text for s in p.spans] == ['The team needs you to build tools.']
    assert p.spans[0].modality_hint == 'required'


def test_plus_connector_and_explicit_preference_override() -> None:
    p = canonical_posting({'key': 'plus', 'description_html':
        '<h2>Required Qualifications</h2><p>BS plus 5 years required</p>'
        '<p>A plus 5 years</p><p>BS preferred</p><p>No degree required</p>'}, {})
    assert p.spans[0].tree is not None and p.spans[0].tree.op == 'AND'
    assert [a.kind for a in p.spans[1].atoms] == ['years']
    assert [s.modality_hint for s in p.spans] == ['required', 'preferred', 'preferred', 'preferred']


def test_ats_bold_section_labels_preserve_work_exclusion() -> None:
    p = canonical_posting({'key': 'ats', 'description_html':
        '<p><b>What we are looking for:</b></p><p>Communication skills</p>'
        '<p><b>Primary responsibilities:</b></p><p><b>Note:</b></p><p>Must build tools.</p>'
        '<p><b>Qualifications:</b></p><p><b>BS required</b></p>'
        '<p><b>Compensation Overview:</b></p><p>Lunch included.</p>'}, {})
    assert [s.text for s in p.spans] == ['Communication skills', 'BS required']
