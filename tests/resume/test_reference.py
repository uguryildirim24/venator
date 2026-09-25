from pathlib import Path
import copy
import json

import pdfplumber
import pytest
import reportlab
from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from venator.resume.reference import (
    capture_reference,
    render_reference,
    preflight_reference,
    ReferenceLayoutError,
)

FONT_DIR = Path(reportlab.__file__).parent / "fonts"


@pytest.fixture
def source(tmp_path):
    for style, file in [
        ("regular", "Vera.ttf"),
        ("bold", "VeraBd.ttf"),
        ("italic", "VeraIt.ttf"),
    ]:
        pdfmetrics.registerFont(TTFont("TestRef-" + style, str(FONT_DIR / file)))
    path = tmp_path / "source.pdf"
    c = Canvas(str(path), pagesize=(612, 792))

    def text(text, x, y, style="regular", size=10.5):
        c.setFont("TestRef-" + style, size)
        c.drawString(x, 792 - y, text)

    text("Ari Chen", 260, 36, "bold", 16)
    text("Boston • ari@example.test", 225, 52)
    text("EDUCATION", 34, 72, "bold", 11)
    c.setLineWidth(0.75)
    c.line(34, 716, 578, 716)
    text("Example University", 34, 92, "bold")
    text("May 2027", 515, 92)
    text("Science degree", 34, 103.4)
    text("EXPERIENCE", 34, 122, "bold", 11)
    c.line(34, 666, 578, 666)
    text("Example Laboratory", 34, 142, "bold")
    text("2026", 548, 142)
    text("Student Assistant - Boston", 34, 153.4, "italic")
    text("•", 40, 164.8)
    text("Assisted with 12 samples.", 52, 164.8)
    c.save()
    return path


@pytest.fixture
def resume():
    return {
        "name": "Ari Chen",
        "contact": {"location": "Boston", "email": "ari@example.test"},
        "sections": [
            {"key": "education", "title": "EDUCATION", "kind": "education"},
            {"key": "experience", "title": "EXPERIENCE", "kind": "entries"},
        ],
        "education": [
            {
                "org": "Example University",
                "date": "May 2027",
                "degree": "Science degree",
            }
        ],
        "experience": [
            {
                "org": "Example Laboratory",
                "dates": "2026",
                "role": "Student Assistant",
                "location": "Boston",
                "bullets": ["Assisted with 12 samples."],
            }
        ],
    }


def test_embedded_fonts_are_remapped_and_layout_is_portable(source, resume, tmp_path):
    before = source.read_bytes()
    directory = tmp_path / "reference"
    metadata = capture_reference(source, directory)
    assert source.read_bytes() == before
    assert set(metadata["fonts"]) == {"regular", "bold", "italic"}
    assert all(
        Path(asset["file"]).name == asset["file"]
        for asset in metadata["fonts"].values()
    )
    source.unlink()  # portability requires no access to the uploaded path
    output = tmp_path / "out.pdf"
    render_reference(resume, output, directory)
    assert preflight_reference(resume, directory)["pages"] == 1
    with pdfplumber.open(output) as document:
        page = document.pages[0]
        text = page.extract_text()
        assert "Ari Chen" in text and "Assisted with 12 samples." in text
        assert text.index("EDUCATION") < text.index("EXPERIENCE")
        assert len(document.pages) == 1 and len(page.lines) == 2
        assert all(abs(line["linewidth"] - 0.75) < 0.01 for line in page.lines)
        words = page.extract_words(extra_attrs=["size", "fontname"])
        assert any(
            word["text"] == "Assistant" and "Oblique" in word["fontname"]
            for word in words
        )
        assert any(
            word["text"] == "Assisted" and word["size"] == 10.5 for word in words
        )
        assert max(c["x1"] for c in page.chars) < 579


def test_full_same_font_expansion_supports_new_characters(source, resume, tmp_path):
    directory = tmp_path / "reference"
    capture_reference(source, directory, font_directory=FONT_DIR)
    resume["experience"][0]["bullets"] = ["Measured density 3.3 × 10⁷ CFU/mL."]
    output = tmp_path / "out.pdf"
    render_reference(resume, output, directory)
    with pdfplumber.open(output) as d:
        seven = [c for c in d.pages[0].chars if c["text"] == "7" and c["size"] < 10]
        assert seven and abs(seven[0]["size"] - 10.5 * 0.58) < 0.01
    assert all(
        v["unicode_to_code"] is None
        for v in json.loads((directory / "layout.json").read_text())["fonts"].values()
    )


def test_missing_subset_glyph_does_not_replace_output(source, resume, tmp_path):
    directory = tmp_path / "reference"
    capture_reference(source, directory)
    output = tmp_path / "out.pdf"
    output.write_bytes(b"previous")
    resume["name"] = "Ari Ω"
    with pytest.raises(ReferenceLayoutError, match="lacks characters"):
        render_reference(resume, output, directory)
    assert output.read_bytes() == b"previous"


@pytest.mark.parametrize("failure", ["overflow", "date-overlap", "changed-font"])
def test_layout_and_integrity_failures_preserve_existing_output(
    source, resume, tmp_path, failure
):
    directory = tmp_path / "reference"
    capture_reference(source, directory, font_directory=FONT_DIR)
    output = tmp_path / "out.pdf"
    output.write_bytes(b"previous")
    if failure == "overflow":
        resume["experience"][0]["bullets"] = ["A long supported sentence. " * 20] * 20
    elif failure == "date-overlap":
        resume["experience"][0]["org"] = "Very long employer name " * 20
    else:
        (directory / "bold.ttf").write_bytes(b"changed")
    with pytest.raises(ReferenceLayoutError):
        render_reference(resume, output, directory)
    assert output.read_bytes() == b"previous"


def test_multi_page_source_is_rejected(source, tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    page = PdfReader(source).pages[0]
    writer.add_page(page)
    writer.add_page(page)
    multiple = tmp_path / "multiple.pdf"
    writer.write(multiple)
    with pytest.raises(ReferenceLayoutError, match="one unrotated"):
        capture_reference(multiple, tmp_path / "reference")


def test_confirmed_sections_and_entry_order_are_preserved(source, resume, tmp_path):
    directory = tmp_path / "reference"
    capture_reference(source, directory, font_directory=FONT_DIR)
    resume["sections"].reverse()
    hidden = copy.deepcopy(resume["experience"][0])
    hidden.update(org="Hidden draft", draft=True)
    resume["experience"].insert(0, hidden)
    output = tmp_path / "out.pdf"
    render_reference(resume, output, directory)
    text = PdfReader(output).pages[0].extract_text()
    assert text.index("EXPERIENCE") < text.index("EDUCATION") and "Hidden" not in text


def test_parallel_prose_columns_are_rejected(source, tmp_path):
    from io import BytesIO
    from pypdf import PdfWriter

    overlay = BytesIO()
    canvas = Canvas(overlay, pagesize=(612, 792))
    canvas.setFont("TestRef-regular", 10.5)
    canvas.drawString(420, 792 - 164.8, "Second column")
    canvas.save()
    writer = PdfWriter(clone_from=source)
    writer.pages[0].merge_page(PdfReader(overlay).pages[0])
    columns = tmp_path / "columns.pdf"
    writer.write(columns)
    with pytest.raises(ReferenceLayoutError, match="Multiple-column"):
        capture_reference(columns, tmp_path / "reference")


def test_same_family_expansion_rejects_incomplete_face_set(source, tmp_path):
    import shutil

    fonts = tmp_path / "fonts"
    fonts.mkdir()
    shutil.copy(FONT_DIR / "Vera.ttf", fonts / "only-regular.ttf")
    with pytest.raises(ReferenceLayoutError, match="No matching full bold"):
        capture_reference(source, tmp_path / "reference", font_directory=fonts)


def _reference_cli(source_path, directory):
    import subprocess
    import sys

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "venator.resume.reference",
            str(source_path),
            str(directory),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_cli_success_retains_staged_source_and_records_supported_status(
    source, tmp_path
):
    import shutil

    directory = tmp_path / "stage"
    directory.mkdir()
    staged = directory / "source.pdf"
    shutil.copyfile(source, staged)
    before = staged.read_bytes()
    result = _reference_cli(staged, directory)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"supported": True}
    assert json.loads((directory / "status.json").read_text()) == {"supported": True}
    assert staged.read_bytes() == before and source.read_bytes() == before
    assert (directory / "layout.json").is_file()


def test_cli_unsupported_pdf_retains_source_removes_stale_assets_and_records_reason(
    tmp_path,
):
    directory = tmp_path / "stage"
    directory.mkdir()
    source = directory / "source.pdf"
    content = b"unsupported synthetic document"
    source.write_bytes(content)
    for filename in ("layout.json", "regular.ttf", "bold.ttf", "italic.ttf"):
        (directory / filename).write_bytes(b"stale asset")
    result = _reference_cli(source, directory)
    assert result.returncode == 0
    outcome = json.loads(result.stdout)
    assert outcome["supported"] is False and outcome["reason"]
    assert json.loads((directory / "status.json").read_text()) == outcome
    assert source.read_bytes() == content
    assert all(
        not (directory / filename).exists()
        for filename in ("layout.json", "regular.ttf", "bold.ttf", "italic.ttf")
    )


@pytest.mark.parametrize("failure", ["missing-source", "destination-is-file"])
def test_cli_io_failure_is_nonzero_and_does_not_expose_source_path(
    source, tmp_path, failure
):
    directory = tmp_path / "stage"
    if failure == "missing-source":
        source = tmp_path / "private-person-name-missing.pdf"
    else:
        directory.write_bytes(b"existing file")
    result = _reference_cli(source, directory)
    assert result.returncode == 1 and not result.stdout
    assert "Could not read the uploaded PDF or save its layout." in result.stderr
    assert str(source) not in result.stderr
    if failure == "destination-is-file":
        assert directory.read_bytes() == b"existing file"


def test_education_location_must_not_overlap_date(source, resume, tmp_path):
    from venator.resume.reference import ReferenceOverflowError

    directory = tmp_path / "reference"
    capture_reference(source, directory, font_directory=FONT_DIR)
    # Fit within the page, but collide with the right-aligned May 2027 date.
    resume["education"][0]["location"] = "Boston metropolitan region " * 3
    with pytest.raises(ReferenceOverflowError, match="date"):
        preflight_reference(resume, directory)


def test_render_preserves_measured_bullet_size(source, resume, tmp_path):
    directory = tmp_path / "reference"
    capture_reference(source, directory, font_directory=FONT_DIR)
    output = tmp_path / "render.pdf"
    render_reference(resume, output, directory)
    with pdfplumber.open(source) as original, pdfplumber.open(output) as rendered:
        old = [c for c in original.pages[0].chars if c["text"] == "•" and c["x0"] < 100]
        new = [c for c in rendered.pages[0].chars if c["text"] == "•" and c["x0"] < 100]
        assert new[0]["size"] == pytest.approx(old[0]["size"], abs=0.01)


def test_render_preserves_reference_contact_alignment(source, resume, tmp_path):
    directory = tmp_path / "reference"
    capture_reference(source, directory, font_directory=FONT_DIR)
    output = tmp_path / "render.pdf"
    render_reference(resume, output, directory)
    with pdfplumber.open(source) as original, pdfplumber.open(output) as rendered:
        old = next(
            line
            for line in original.pages[0].extract_text_lines()
            if "ari@example.test" in line["text"]
        )
        new = next(
            line
            for line in rendered.pages[0].extract_text_lines()
            if "ari@example.test" in line["text"]
        )
        assert new["x0"] == pytest.approx(old["x0"], abs=0.2)


def test_noncenter_header_anchor_survives_changed_name_and_contact(
    source, resume, tmp_path
):
    directory = tmp_path / "reference"
    metadata = capture_reference(source, directory, font_directory=FONT_DIR)
    assert metadata["layout"]["name_align"] == "left"
    assert metadata["layout"]["contact_align"] == "left"
    resume["name"] = "Ari Chen Student"
    resume["contact"]["phone"] = "555-0100"
    output = tmp_path / "render.pdf"
    render_reference(resume, output, directory)
    with pdfplumber.open(output) as rendered:
        lines = rendered.pages[0].extract_text_lines()
        name = next(line for line in lines if "Ari Chen Student" in line["text"])
        contact = next(line for line in lines if "555-0100" in line["text"])
        assert name["x0"] == pytest.approx(260, abs=0.2)
        assert contact["x0"] == pytest.approx(225, abs=0.2)
