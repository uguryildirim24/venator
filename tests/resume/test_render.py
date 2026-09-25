"""The renderer reproduces the Owner's reference layout, glyph for glyph.

profiles/example/resume.yaml states that the render must reproduce the reference
PDF exactly, and render.py carries constants measured off it to 0.1pt. Tests
that only check "it produced a PDF" would let a reflow through, so this one
pins every glyph's position and every rule's extent against a golden capture
taken before the renderer was made Profile-driven.

Font names are deliberately not compared. register_fonts only ever loads Times
New Roman / Arial or the Liberation faces, which are metrically compatible with
them, so the geometry is the same either way — and if a family with different
metrics were ever loaded, the coordinates below would move and this test would
say so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pdfplumber
import pytest
import yaml

from venator.resume.render import find_font, font_search_path, render

REPOSITORY = Path(__file__).parents[2]
GOLDEN = Path(__file__).parent / "fixtures" / "example-layout.json"
TOLERANCE = 0.05  # well inside the 0.1pt the measured constants are stated to


def _fonts_are_installed() -> bool:
    """Whether every face the renderer needs exists somewhere on the search path."""
    from venator.resume.render import FONT_FILES

    search_path = font_search_path()
    return all(find_font(filenames, search_path) is not None for filenames in FONT_FILES.values())


def layout(pdf_path: Path) -> list[list[object]]:
    """Every glyph and rule, as position and size — the layout, not the bytes."""
    elements: list[list[object]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for char in page.chars:
                elements.append(
                    ["char", char["text"], round(char["x0"], 3), round(char["top"], 3), round(char["size"], 3)]
                )
            for line in page.lines:
                elements.append(
                    ["rule", "", round(line["x0"], 3), round(line["top"], 3), round(line["x1"], 3)]
                )
    return elements


def render_layout(resume_path: Path, out_path: Path) -> list[list[object]]:
    render(yaml.safe_load(resume_path.read_text(encoding="utf-8")), out_path)
    return layout(out_path)


needs_fonts = pytest.mark.skipif(
    not _fonts_are_installed(),
    reason="no Times New Roman / Arial or Liberation faces installed to render with",
)


@needs_fonts
def test_owner_resume_renders_the_reference_layout_unchanged(tmp_path: Path) -> None:
    produced = render_layout(REPOSITORY / "profiles" / "example" / "resume.yaml", tmp_path / "resume.pdf")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert len(produced) == len(expected), "the render gained or lost elements"
    for index, (actual, golden) in enumerate(zip(produced, expected)):
        assert actual[0] == golden[0], f"element {index} changed kind"
        assert actual[1] == golden[1], f"element {index} changed text"
        for axis, (a, g) in enumerate(zip(actual[2:], golden[2:])):
            assert abs(a - g) <= TOLERANCE, f"element {index} moved on axis {axis}: {a} vs {g}"


@needs_fonts
def test_render_is_deterministic(tmp_path: Path) -> None:
    resume = REPOSITORY / "profiles" / "example" / "resume.yaml"

    assert render_layout(resume, tmp_path / "one.pdf") == render_layout(resume, tmp_path / "two.pdf")


@needs_fonts
def test_a_resume_without_a_gpa_or_coursework_renders_rather_than_raising(tmp_path: Path) -> None:
    """The scaffold Profile has neither; both were required keys before."""
    example = REPOSITORY / "profiles" / "example" / "resume.yaml"
    resume = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert "gpa" not in resume["education"][0] and "coursework" not in resume["education"][0]

    render(resume, tmp_path / "example.pdf")
    text = "".join(
        str(element[1]) for element in layout(tmp_path / "example.pdf")
    ).replace(" ", "")

    assert "CumulativeGPA" not in text
    assert "RelevantCoursework" not in text
    assert "EDUCATION" in text
    # Sections with no entries are skipped, header and all.
    assert "MEMBERSHIPS" not in text


@needs_fonts
def test_a_variant_toggles_experience_entries_and_leaves_the_facts_alone(tmp_path: Path) -> None:
    """Tailoring toggles experience entries; education is a fact the resume states."""
    resume = yaml.safe_load(
        (REPOSITORY / "profiles" / "example" / "resume.yaml").read_text(encoding="utf-8")
    )
    resume["education"][0]["include"] = False
    resume["technical_proficiencies"][0]["include"] = False
    resume["experience"][0]["include"] = False

    render(resume, tmp_path / "toggled.pdf")
    drawn = "".join(
        str(element[1]) for element in layout(tmp_path / "toggled.pdf") if element[0] == "char"
    ).replace(" ", "")

    assert resume["education"][0]["org"].replace(" ", "") in drawn
    assert resume["technical_proficiencies"][0]["label"].replace(" ", "") in drawn
    assert resume["experience"][0]["org"].replace(" ", "") not in drawn


def test_missing_fonts_name_what_was_looked_for_and_how_to_fix_it(tmp_path: Path) -> None:
    from venator.resume.render import FONT_DIR_ENV, register_fonts

    with pytest.raises(FileNotFoundError) as error:
        register_fonts(tmp_path / "no-fonts-here")

    message = str(error.value)
    assert "Times New Roman.ttf" in message
    assert str(tmp_path / "no-fonts-here") in message
    assert FONT_DIR_ENV in message
