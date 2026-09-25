"""Render a Profile's resume.yaml into a one-page PDF.

Every vertical pitch and x-position below was measured with pdfplumber from the
reference layout this renderer reproduces; coordinates are "top of glyph" in
points, converted to ReportLab baselines at draw time.

Section titles and their order are Profile data (resume.yaml: sections), and a
resume without a GPA or a coursework line simply omits it.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

FONT_DIR_ENV = "VENATOR_FONT_DIR"
# Searched in order. The Liberation faces are metrically compatible with Times
# New Roman and Arial, so a Linux render keeps the measured layout rather than
# only approximating it.
FONT_SEARCH_PATH = (
    Path("/System/Library/Fonts/Supplemental"),
    Path("/Library/Fonts"),
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts/truetype/msttcorefonts"),
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".local" / "share" / "fonts",
)
R, B, I, BULLET_FONT = "TNR", "TNR-Bold", "TNR-Italic", "ArialBullet"
FONT_FILES = {
    R: ("Times New Roman.ttf", "times.ttf", "LiberationSerif-Regular.ttf"),
    B: ("Times New Roman Bold.ttf", "timesbd.ttf", "LiberationSerif-Bold.ttf"),
    I: ("Times New Roman Italic.ttf", "timesi.ttf", "LiberationSerif-Italic.ttf"),
    BULLET_FONT: ("Arial.ttf", "arial.ttf", "LiberationSans-Regular.ttf"),
}

# Which section kinds a Tailoring variant may toggle entries out of. The
# Tailoring contract is that a variant turns *experience* entries on and off
# (src/venator/tailor/variants.py); education and the proficiencies table are
# facts the resume always states, and applying `include` to them here would be
# an undocumented change to that contract. Extending variants to reach them is
# a deliberate decision for another change, not a side effect of this one.
TAILORABLE_KINDS = frozenset({"entries", "titles"})

# Section titles and order when a resume.yaml does not name its own.
DEFAULT_SECTIONS = (
    {"key": "education", "title": "EDUCATION", "kind": "education"},
    {"key": "technical_proficiencies", "title": "TECHNICAL PROFICIENCIES", "kind": "labeled"},
    {
        "key": "experience",
        "title": "RELEVANT EXPERIENCE",
        "kind": "entries",
        "heading": "org",
        "subheading": "role",
    },
    {"key": "memberships", "title": "MEMBERSHIPS AND ASSOCIATIONS", "kind": "titles"},
    {
        "key": "campus_involvement",
        "title": "CAMPUS INVOLVEMENT",
        "kind": "entries",
        "heading": "role",
        "subheading": "org",
    },
)
SECTION_KINDS = frozenset({"education", "labeled", "entries", "titles"})

PAGE_W, PAGE_H = 612.0, 792.0
LEFT, RIGHT = 27.0, 577.5
CENTER = 306.0
SIZE = 10.0
NAME_SIZE = 16.0
ASCENT = 0.781  # empirical: matches pdfplumber "top" of the reference within 0.1pt

NAME_TOP = 36.9
CONTACT_TOP = 54.0
FIRST_HEADER_TOP = 80.5
PLAIN_PITCH = 13.2       # education / proficiency lines
TIGHT_PITCH = 11.5       # org->role and wrapped bullet lines
SUB_TO_BULLETS = 14.5    # role line -> first bullet
ENTRY_GAP = 14.5         # last bullet -> next entry title
CONTENT_TO_HEADER = 21.8
HEADER_TO_CONTENT = 22.2
DATE_RIGHT = 575.0       # right-aligned dates end here (visible glyph edge in reference)
RULE_OFFSET = 15.0       # header top -> its full-width rule
RULE_ABOVE_FIRST = 8.0   # extra rule above the first section header (table top border)
RULE_X0, RULE_X1 = 26.0, 573.0
BULLET_X = 45.0
BULLET_TEXT_X = 63.0

SUPERSCRIPTS = {c: d for c, d in zip("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")}
SUPER_SCALE = 0.58
SUPER_RISE = 3.3

Run = tuple[str, float, str, float]  # font, size, text, rise


def font_search_path(font_dir: Path | None = None) -> tuple[Path, ...]:
    """An explicit directory, else $VENATOR_FONT_DIR, else the platform defaults."""
    if font_dir is not None:
        return (Path(font_dir),)
    override = os.environ.get(FONT_DIR_ENV)
    return (Path(override),) if override else FONT_SEARCH_PATH


def find_font(filenames: tuple[str, ...], search_path: tuple[Path, ...]) -> Path | None:
    for directory in search_path:
        for filename in filenames:
            candidate = directory / filename
            if candidate.is_file():
                return candidate
    return None


def register_fonts(font_dir: Path | None = None) -> None:
    search_path = font_search_path(font_dir)
    for name, filenames in FONT_FILES.items():
        found = find_font(filenames, search_path)
        if found is None:
            raise FileNotFoundError(
                f"no font file for {name}: looked for "
                f"{', '.join(filenames)} in {', '.join(str(d) for d in search_path)}. "
                f"Install one of them, or set {FONT_DIR_ENV} to a directory holding them."
            )
        pdfmetrics.registerFont(TTFont(name, str(found)))


def smarten(text: str) -> str:
    """Typographic apostrophes/quotes, as the reference document uses."""
    out: list[str] = []
    open_quote = True
    for ch in text:
        if ch == "'":
            out.append("’")
        elif ch == '"':
            out.append("“" if open_quote else "”")
            open_quote = not open_quote
        else:
            out.append(ch)
    return "".join(out)


def runs(text: str, font: str = R, size: float = SIZE) -> list[Run]:
    """Split a string into runs, rendering unicode superscripts as risen digits."""
    result: list[Run] = []
    plain = ""
    for ch in smarten(text):
        if ch in SUPERSCRIPTS:
            if plain:
                result.append((font, size, plain, 0.0))
                plain = ""
            result.append((font, size * SUPER_SCALE, SUPERSCRIPTS[ch], SUPER_RISE))
        else:
            plain += ch
    if plain:
        result.append((font, size, plain, 0.0))
    return result


def runs_width(run_list: list[Run]) -> float:
    return sum(pdfmetrics.stringWidth(t, f, s) for f, s, t, _ in run_list)


def text_width(text: str, font: str = R, size: float = SIZE) -> float:
    return runs_width(runs(text, font, size))


def wrap(text: str, width: float, font: str = R, size: float = SIZE) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if not current or text_width(candidate, font, size) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


class Page:
    def __init__(self, canvas: Canvas) -> None:
        self.canvas = canvas

    def draw_runs(self, x: float, top: float, run_list: list[Run], nominal: float = SIZE) -> None:
        baseline = PAGE_H - top - ASCENT * nominal
        cursor = x
        for font, size, text, rise in run_list:
            self.canvas.setFont(font, size)
            self.canvas.drawString(cursor, baseline + rise, text)
            cursor += pdfmetrics.stringWidth(text, font, size)

    def draw_centered(self, top: float, run_list: list[Run], nominal: float = SIZE) -> None:
        self.draw_runs(CENTER - runs_width(run_list) / 2, top, run_list, nominal)

    def draw_right(self, top: float, run_list: list[Run]) -> None:
        self.draw_runs(DATE_RIGHT - runs_width(run_list), top, run_list)

    def rule(self, top: float) -> None:
        y = PAGE_H - top
        self.canvas.setLineWidth(0)  # hairline, as the reference's table borders
        self.canvas.line(RULE_X0, y, RULE_X1, y)


class Renderer:
    def __init__(self, canvas: Canvas) -> None:
        self.page = Page(canvas)
        self.top = 0.0

    def header(self, title: str, first: bool = False) -> None:
        self.top = FIRST_HEADER_TOP if first else self.top + CONTENT_TO_HEADER
        self.page.draw_runs(LEFT, self.top, runs(title, B))
        if first:
            self.page.rule(self.top - RULE_ABOVE_FIRST)
        self.page.rule(self.top + RULE_OFFSET)
        self.top += HEADER_TO_CONTENT

    def labeled_line(self, label: str, rest: str) -> None:
        self.page.draw_runs(LEFT, self.top, runs(f"{label}: ", B) + runs(rest))
        self.top += PLAIN_PITCH

    def title_line(self, title_runs: list[Run], date: str) -> None:
        self.page.draw_runs(LEFT, self.top, title_runs)
        self.page.draw_right(self.top, runs(date))

    def sub_line(self, main: str, location: str) -> None:
        self.top += TIGHT_PITCH
        self.page.draw_runs(LEFT, self.top, runs(f"{main} ", I) + runs(f"- {location}"))

    def bullets(self, items: list[str]) -> None:
        self.top += SUB_TO_BULLETS
        width = RIGHT - BULLET_TEXT_X
        for item in items:
            self.page.draw_runs(BULLET_X, self.top, [(BULLET_FONT, SIZE, "●", 0.0)])
            for j, line in enumerate(wrap(item, width)):
                if j > 0:
                    self.top += TIGHT_PITCH
                self.page.draw_runs(BULLET_TEXT_X, self.top, runs(line))
            self.top += TIGHT_PITCH
        self.top -= TIGHT_PITCH  # cursor rests on the last drawn line


def included(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("include", True)]


def sections(profile: dict) -> list[dict]:
    """The sections to render, in order — the resume's own list or the default."""
    configured = profile.get("sections")
    if not configured:
        return [dict(section) for section in DEFAULT_SECTIONS]
    if not isinstance(configured, list):
        raise ValueError("resume.yaml: sections must be a list")
    for index, section in enumerate(configured):
        if not isinstance(section, dict) or not section.get("key") or not section.get("title"):
            raise ValueError(f"resume.yaml: sections[{index}] needs a key and a title")
        if section.get("kind", "entries") not in SECTION_KINDS:
            raise ValueError(
                f"resume.yaml: sections[{index}].kind must be one of {sorted(SECTION_KINDS)}"
            )
    return [dict(section) for section in configured]


def _render_education(r: Renderer, entries: list[dict]) -> None:
    for edu in entries:
        r.title_line(runs(f"{edu['org']} ", B) + runs(f"- {edu['location']}"), edu["date"])
        r.top += PLAIN_PITCH
        r.page.draw_runs(LEFT, r.top, runs(edu["degree"], I))
        r.top += PLAIN_PITCH
        if edu.get("gpa"):
            r.labeled_line("Cumulative GPA", edu["gpa"])
        if edu.get("coursework"):
            r.page.draw_runs(
                LEFT, r.top, runs("Relevant Coursework: ", B) + runs(edu["coursework"])
            )


def _render_labeled(r: Renderer, entries: list[dict]) -> None:
    for index, row in enumerate(entries):
        r.page.draw_runs(LEFT, r.top, runs(f"{row['label']}: ", B) + runs(row["items"]))
        if index < len(entries) - 1:
            r.top += PLAIN_PITCH


def _render_entries(r: Renderer, entries: list[dict], heading: str, subheading: str) -> None:
    for index, entry in enumerate(entries):
        if index > 0:
            r.top += ENTRY_GAP
        r.title_line(runs(entry[heading], B), entry["dates"])
        r.sub_line(entry[subheading], entry["location"])
        r.bullets(entry["bullets"])


def _render_titles(r: Renderer, entries: list[dict]) -> None:
    for entry in entries:
        r.title_line(runs(entry["org"], B), entry["dates"])


def render(profile: dict, out_path: Path, *, font_dir: Path | None = None) -> None:
    register_fonts(font_dir)
    canvas = Canvas(str(out_path), pagesize=(PAGE_W, PAGE_H))
    canvas.setTitle(profile["name"])
    page = Page(canvas)
    r = Renderer(canvas)

    page.draw_centered(NAME_TOP, runs(profile["name"], B, NAME_SIZE), NAME_SIZE)
    contact = profile["contact"]
    contact_text = " • ".join(
        [contact["location"], contact["phone"], contact["email"], contact["linkedin"]]
    )
    page.draw_centered(CONTACT_TOP, runs(contact_text))

    first = True
    for section in sections(profile):
        kind = section.get("kind", "entries")
        entries = list(profile.get(section["key"]) or [])
        if kind in TAILORABLE_KINDS:
            entries = included(entries)
        if not entries:
            continue
        r.header(section["title"], first=first)
        first = False
        if kind == "education":
            _render_education(r, entries)
        elif kind == "labeled":
            _render_labeled(r, entries)
        elif kind == "titles":
            _render_titles(r, entries)
        else:
            _render_entries(
                r, entries, section.get("heading", "org"), section.get("subheading", "role")
            )

    canvas.showPage()
    canvas.save()

    bottom = r.top + SIZE * 1.15
    if bottom > PAGE_H:
        print(f"WARNING: content overflows the page by {bottom - PAGE_H:.0f}pt — cut or trim an entry")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path, help="path to resume.yaml")
    parser.add_argument("-o", "--out", type=Path, default=Path("build/resume.pdf"))
    parser.add_argument(
        "--font-dir",
        type=Path,
        default=None,
        help=f"directory holding the TTF faces (default: ${FONT_DIR_ENV}, else the platform's)",
    )
    args = parser.parse_args()
    profile = yaml.safe_load(args.profile.read_text(encoding="utf-8"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    render(profile, args.out, font_dir=args.font_dir)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
