"""Prepare reviewable application documents from confirmed Profile facts.

A first selected-provider call drafts source-linked resume bullets and an
optional personal letter. A second call checks every generated passage against
its original facts. Uncertain or unsupported claims prevent publication; the
original Profile identity, education and employment facts remain unchanged.

The output is an immutable version directory followed by one atomic manifest
replacement.  A failed completion or render therefore cannot replace a
previously reviewable application.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from venator.discover.store import posting_revision
from venator.match.store import filters_version
from venator.profile import Profile
from venator.profile.facts import confirmed as _confirmed
from venator.resume.render import DEFAULT_SECTIONS, SUPERSCRIPTS, register_fonts
from venator.tailor.draft import CheckedDraft, DRAFT_SCHEMA, SUPPORTED_PROVIDERS, generate_and_check


PREPARATION_REVISION = 8

# These are control fields in resume.yaml.  They decide whether a fact may be
# selected, but are never copied into the rendered document.
_CONTROL_FIELDS = frozenset(
    {
        "id",
        "source_id",
        "entry_id",
        "confirmed",
        "owner_confirmed",
        "draft",
        "status",
        "include",
        "primary",
    }
)
SELECTION_SCHEMA = DRAFT_SCHEMA

@dataclass(frozen=True)
class _Bullet:
    identifier: str
    text: str


@dataclass(frozen=True)
class _Entry:
    identifier: str
    section: str
    spec: Mapping[str, object]
    source: object
    bullets: tuple[_Bullet, ...]


@dataclass(frozen=True)
class _Line:
    text: str
    kind: str = "body"


@dataclass(frozen=True)
class _Selection:
    entries: tuple[_Entry, ...]
    bullets: Mapping[str, tuple[_Bullet, ...]]


def _plain_copy(value: object) -> object:
    """Copy YAML-shaped data without trying to pickle MappingProxyType."""

    if isinstance(value, Mapping):
        return {str(key): _plain_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_copy(item) for item in value]
    if isinstance(value, set):
        return sorted((_plain_copy(item) for item in value), key=str)
    return value


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    return str(value)


def _flatten_text(value: object) -> list[str]:
    scalar = _text(value)
    if scalar is not None:
        return [scalar]
    if isinstance(value, Mapping):
        result: list[str] = []
        if not _confirmed(value):
            return []
        for key, item in value.items():
            if key not in _CONTROL_FIELDS:
                result.extend(_flatten_text(item))
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_flatten_text(item))
        return result
    return []


def _identifier(value: object, fallback: str) -> str:
    if isinstance(value, Mapping):
        for key in ("id", "source_id", "entry_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return fallback


def _resume_mapping(profile: Profile) -> Mapping[str, object]:
    if not isinstance(profile, Profile):
        raise TypeError("profile must be a Profile")
    return profile.resume


def _section_specs(resume: Mapping[str, object]) -> list[dict[str, object]]:
    configured = resume.get("sections")
    if isinstance(configured, list) and configured:
        specs = [dict(item) for item in configured if isinstance(item, Mapping) and item.get("key")]
    else:
        specs = [dict(item) for item in DEFAULT_SECTIONS]

    known = {str(spec["key"]) for spec in specs}
    for key, value in resume.items():
        if key in known or key in {"sections", "name", "contact"}:
            continue
        if isinstance(value, list):
            specs.append({"key": str(key), "title": str(key).replace("_", " ").upper(), "kind": "entries"})
            known.add(str(key))
    return specs


def _bullet_text(value: object) -> str | None:
    scalar = _text(value)
    if scalar is not None:
        return scalar
    if not isinstance(value, Mapping):
        return None
    for key in ("text", "content", "value", "bullet", "description"):
        candidate = _text(value.get(key))
        if candidate is not None:
            return candidate
    return None


def _catalogue(resume: Mapping[str, object]) -> tuple[list[dict[str, object]], tuple[_Entry, ...], int]:
    specs = _section_specs(resume)
    entries: list[_Entry] = []
    identifiers: set[str] = set()
    excluded = 0
    resume_allowed = _confirmed(resume)
    for spec in specs:
        section = str(spec["key"])
        raw_entries = resume.get(section)
        if isinstance(raw_entries, Mapping):
            raw_items: list[object] = [raw_entries]
        elif isinstance(raw_entries, list):
            raw_items = list(raw_entries)
        else:
            continue
        for index, raw in enumerate(raw_items):
            entry_id = _identifier(raw, f"{section}:{index}")
            if entry_id in identifiers:
                raise ValueError(f"resume source identifier is duplicated: {entry_id}")
            identifiers.add(entry_id)
            if not resume_allowed or not _confirmed(raw):
                excluded += 1
                # We intentionally do not expose excluded IDs to the model.
                continue
            bullets: list[_Bullet] = []
            raw_bullets = raw.get("bullets") if isinstance(raw, Mapping) else None
            if isinstance(raw_bullets, list):
                for bullet_index, raw_bullet in enumerate(raw_bullets):
                    bullet_id = _identifier(raw_bullet, f"{entry_id}:bullet:{bullet_index}")
                    if bullet_id in identifiers:
                        raise ValueError(f"resume source identifier is duplicated: {bullet_id}")
                    identifiers.add(bullet_id)
                    text = _bullet_text(raw_bullet)
                    if text is None:
                        continue
                    if not _confirmed(raw_bullet):
                        excluded += 1
                        continue
                    bullets.append(_Bullet(bullet_id, text))
            entries.append(_Entry(entry_id, section, spec, _source_without_controls(raw), tuple(bullets)))
    return specs, tuple(entries), excluded


def _source_without_controls(value: object) -> object:
    """Strip controls and withheld nested facts before prompting or rendering."""
    if isinstance(value, Mapping):
        if not _confirmed(value):
            return None
        return {
            str(key): _source_without_controls(item)
            for key, item in value.items()
            if key not in _CONTROL_FIELDS and key != "bullets" and _confirmed(item)
        }
    if isinstance(value, (list, tuple)):
        return [_source_without_controls(item) for item in value if _confirmed(item)]
    return _plain_copy(value)


def _prompt_sources(entries: Sequence[_Entry]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for entry in entries:
        result.append(
            {
                "entry_id": entry.identifier,
                "section": entry.section,
                "retain": entry.section == "education" or entry.spec.get("kind") == "education",
                "entry": _source_without_controls(entry.source),
                "bullets": [{"id": bullet.identifier, "text": bullet.text} for bullet in entry.bullets],
            }
        )
    return result


def _selection(draft: CheckedDraft, entries: Sequence[_Entry]) -> _Selection:
    by_id = {entry.identifier: entry for entry in entries}
    selected = tuple(by_id[identifier] for identifier in draft.selected_entry_ids)
    parents = {bullet.identifier: entry.identifier for entry in entries for bullet in entry.bullets}
    grouped: dict[str, list[_Bullet]] = {entry.identifier: [] for entry in selected}
    for bullet in draft.resume_bullets:
        identifier = bullet["source_id"]
        grouped[parents[identifier]].append(_Bullet(identifier, bullet["text"]))
    return _Selection(selected, {identifier: tuple(bullets) for identifier, bullets in grouped.items()})


def _reference_selection(draft: CheckedDraft, entries: Sequence[_Entry]) -> _Selection:
    """Tailor wording without redesigning the uploaded résumé's structure."""
    rewrites = {row["source_id"]: row["text"] for row in draft.resume_bullets}
    return _Selection(tuple(entries), {
        entry.identifier: tuple(_Bullet(bullet.identifier, rewrites.get(bullet.identifier, bullet.text)) for bullet in entry.bullets)
        for entry in entries
    })


def _structured_resume(resume: Mapping[str, object], specs: Sequence[Mapping[str, object]], selection: _Selection) -> dict:
    value = {"name": _text(resume.get("name")), "contact": _source_without_controls(resume.get("contact")),
             "sections": _plain_copy(specs)}
    for entry in selection.entries:
        if not isinstance(entry.source, Mapping):
            raise ValueError("A reference layout needs structured resume entries.")
        row = _plain_copy(entry.source)
        if entry.bullets:
            row["bullets"] = [bullet.text for bullet in selection.bullets.get(entry.identifier, ())]
        value.setdefault(entry.section, []).append(row)
    return value


def _field_line(value: object, *, join: str = ", ") -> str | None:
    values = [item for item in _flatten_text(value) if item != ""]
    return join.join(values) if values else None


def _entry_lines(entry: _Entry, bullets: Sequence[_Bullet]) -> list[_Line]:
    source = entry.source
    if not isinstance(source, Mapping):
        value = _text(source)
        return [_Line(value)] if value else []
    kind = str(entry.spec.get("kind", "entries"))
    if kind == "education":
        known = ("org", "location", "date", "degree", "gpa", "coursework")
    elif kind == "labeled":
        known = ("label", "items")
    elif kind == "titles":
        known = ("org", "dates")
    else:
        known = (
            str(entry.spec.get("heading", "org")),
            str(entry.spec.get("subheading", "role")),
            "location",
            "dates",
        )
    lines: list[_Line] = []
    emitted: set[str] = set()
    for key in known:
        if key in emitted or key not in source:
            continue
        emitted.add(key)
        value = _field_line(source[key])
        if value:
            lines.append(_Line(value))
    for key, value in source.items():
        key = str(key)
        if key in emitted or key in _CONTROL_FIELDS or key == "bullets":
            continue
        value_line = _field_line(value)
        if value_line:
            lines.append(_Line(value_line))
    lines.extend(_Line(f"- {bullet.text}", "bullet") for bullet in bullets)
    return lines


def _resume_lines(
    resume: Mapping[str, object],
    specs: Sequence[Mapping[str, object]],
    selection: _Selection,
) -> tuple[_Line, ...]:
    lines: list[_Line] = []
    name = _text(resume.get("name"))
    if name:
        lines.append(_Line(name, "name"))
    contact = _source_without_controls(resume.get("contact"))
    if isinstance(contact, Mapping):
        values: list[str] = []
        for value in contact.values():
            values.extend(_flatten_text(value))
        if values:
            lines.append(_Line(" | ".join(values), "contact"))

    by_section: dict[str, list[_Entry]] = {}
    for entry in selection.entries:
        by_section.setdefault(entry.section, []).append(entry)
    for spec in specs:
        section = str(spec.get("key", ""))
        chosen = by_section.get(section, [])
        if not chosen:
            continue
        title = _text(spec.get("title")) or section.replace("_", " ").upper()
        if lines and lines[-1].kind != "blank":
            lines.append(_Line("", "blank"))
        lines.append(_Line(title, "heading"))
        for entry in chosen:
            lines.extend(_entry_lines(entry, selection.bullets.get(entry.identifier, ())))
            lines.append(_Line("", "blank"))
        while lines and lines[-1].kind == "blank":
            lines.pop()
    while lines and lines[-1].kind == "blank":
        lines.pop()
    return tuple(lines)


def _cover_letter(name: str, draft: CheckedDraft) -> str:
    paragraphs = ["Dear Hiring Team,", *(paragraph["text"] for paragraph in draft.letter_paragraphs),
                  "Thank you for your consideration.", name]
    return "\n\n".join(paragraphs) + "\n"


def _font_names() -> tuple[str, str]:
    try:
        register_fonts()
        return "TNR", "TNR-Bold"
    except (FileNotFoundError, OSError):
        # ReportLab includes these embeddable fonts; standard Helvetica silently
        # replaces unsupported Unicode with black boxes.
        fonts = Path(reportlab.__file__).parent / "fonts"
        for name, filename in (("VenatorVera", "Vera.ttf"), ("VenatorVeraBold", "VeraBd.ttf")):
            pdfmetrics.registerFont(TTFont(name, str(fonts / filename)))
        return "VenatorVera", "VenatorVeraBold"


def _paragraph(text: str, style: ParagraphStyle) -> Paragraph:
    safe = escape(text).replace("\n", "<br/>")
    # Reuse the original résumé renderer's scientific-notation treatment.
    # Font coverage of superscript seven is not needed to draw a raised seven.
    for superscript, digit in SUPERSCRIPTS.items():
        safe = safe.replace(superscript, f"<super>{digit}</super>")
    return Paragraph(safe or "&nbsp;", style)


def _validate_fonts(lines: Sequence[_Line]) -> tuple[str, str]:
    regular, bold = _font_names()
    for line in lines:
        font = bold if line.kind in {"name", "heading"} else regular
        supported = pdfmetrics.getFont(font).face.charToGlyph
        missing = sorted({ord(character) for character in line.text if not character.isspace() and ord(SUPERSCRIPTS.get(character, character)) not in supported})
        if missing:
            characters = ", ".join(f"U+{point:04X}" for point in missing[:8])
            raise ValueError(f"resume font cannot render {characters}; configure VENATOR_FONT_DIR with fonts covering this text")
    return regular, bold


def _write_pdf(lines: Sequence[_Line], path: Path, *, title: str) -> None:
    regular, bold = _validate_fonts(lines)
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "VenatorResumeBody",
        parent=styles["BodyText"],
        fontName=regular,
        fontSize=10,
        leading=13,
        spaceAfter=2,
        splitLongWords=1,
    )
    bullet = ParagraphStyle(
        "VenatorResumeBullet",
        parent=body,
        leftIndent=14,
        firstLineIndent=-10,
    )
    heading = ParagraphStyle(
        "VenatorResumeHeading",
        parent=body,
        fontName=bold,
        fontSize=11,
        leading=15,
        spaceBefore=8,
        spaceAfter=5,
        keepWithNext=1,
    )
    name = ParagraphStyle(
        "VenatorResumeName",
        parent=body,
        fontName=bold,
        fontSize=16,
        leading=19,
        alignment=TA_CENTER,
        spaceAfter=3,
    )
    contact = ParagraphStyle(
        "VenatorResumeContact",
        parent=body,
        alignment=TA_CENTER,
        spaceAfter=8,
    )
    entry_heading = ParagraphStyle("VenatorEntryHeading", parent=body, fontName=bold, keepWithNext=1)
    entry_metadata = ParagraphStyle("VenatorEntryMetadata", parent=body, keepWithNext=1)
    document = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=0.55 * inch,
        rightMargin=0.55 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
        title=title,
        author="Venator",
    )
    flow: list[object] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.kind == "blank":
            flow.append(Spacer(1, 4))
        elif line.kind == "name":
            flow.append(_paragraph(line.text, name))
        elif line.kind == "contact":
            flow.append(_paragraph(line.text, contact))
        elif line.kind == "heading":
            flow.append(_paragraph(line.text, heading))
        elif line.kind == "bullet":
            flow.append(_paragraph(line.text, bullet))
        else:
            # Source text keeps one field per line for exact comparison. In
            # the PDF, group entry metadata so dates cannot drift to a new
            # page and short fields do not consume half the résumé.
            fields = [line.text]
            while index < len(lines) and lines[index].kind == "body":
                fields.append(lines[index].text)
                index += 1
            followed_by_bullet = index < len(lines) and lines[index].kind == "bullet"
            flow.append(_paragraph(fields[0], entry_heading if len(fields) > 1 or followed_by_bullet else body))
            if len(fields) > 1:
                flow.append(_paragraph(" | ".join(fields[1:]), entry_metadata if followed_by_bullet else body))
    document.build(flow)


def _input_version(profile: Profile, resume: Mapping[str, object], *, include_layout: bool = True) -> str:
    # The public freshness stamp must match application open; this additional
    # fingerprint also protects callers using in-memory Profile changes.
    payload = {"resume": _plain_copy(resume), "constraints": _plain_copy(profile.constraints),
               "targeting": _plain_copy(profile.targeting)}
    reference = profile.directory / "resume-reference"
    if include_layout and reference.exists():
        payload["resume_reference"] = {
            path.relative_to(reference).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(reference.rglob("*")) if path.is_file()
        }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_path(directory: Path) -> Path:
    return directory / "manifest.json"


def _read_manifest(directory: Path) -> dict[str, object] | None:
    try:
        value = json.loads(_manifest_path(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _requested_files(cover_letter: bool) -> tuple[str, ...]:
    return ("resume.pdf", "resume.txt", "letter.txt") if cover_letter else ("resume.pdf", "resume.txt")


def _usable_manifest(
    directory: Path,
    manifest: Mapping[str, object] | None,
    *,
    provider: str,
    posting_version: str,
    profile_version: str,
    input_version: str,
    cover_letter: bool,
) -> dict[str, object] | None:
    if not manifest or manifest.get("prepared") is not True:
        return None
    if (
        manifest.get("provider") != provider
        or manifest.get("posting_version") != posting_version
        or manifest.get("profile_version") != profile_version
        or manifest.get("input_version") != input_version
        or manifest.get("preparation_revision") != PREPARATION_REVISION
    ):
        return None
    version = manifest.get("version")
    if not isinstance(version, str) or not version.isalnum():
        return None
    resume_text = manifest.get("resumeText")
    letter_text = manifest.get("letterText")
    if not isinstance(resume_text, str):
        return None
    if cover_letter and not isinstance(letter_text, str):
        return None
    if not cover_letter and letter_text is not None:
        return None
    version_dir = directory / "versions" / version
    try:
        if manifest.get("file_hashes") != _file_hashes(version_dir, cover_letter):
            return None
        if (version_dir / "resume.txt").read_text(encoding="utf-8") != resume_text:
            return None
        if cover_letter and (version_dir / "letter.txt").read_text(encoding="utf-8") != letter_text:
            return None
    except (OSError, UnicodeError):
        return None
    return dict(manifest)


def _file_hashes(directory: Path, cover_letter: bool) -> dict[str, str]:
    return {filename: hashlib.sha256((directory / filename).read_bytes()).hexdigest()
            for filename in _requested_files(cover_letter)}


def _write_text(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _write_manifest(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _commit_version(
    directory: Path,
    version: str,
    *,
    resume_text: str,
    resume_lines: Sequence[_Line],
    letter_text: str | None,
    title: str,
    pdf_writer: Callable[[Path], None] | None = None,
) -> str:
    versions = directory / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    target_version = version
    target = versions / target_version
    if target.exists():
        # A version directory is immutable.  A collision can only be a partial
        # prior attempt or an astronomically unlikely digest collision; leave
        # it untouched and make a fresh alphanumeric version.
        target_version = f"{version}{uuid.uuid4().hex[:8]}"
        target = versions / target_version
    staging = versions / f".{target_version}.{uuid.uuid4().hex}.tmp"
    staging.mkdir()
    try:
        _write_text(staging / "resume.txt", resume_text)
        if pdf_writer is None:
            _write_pdf(resume_lines, staging / "resume.pdf", title=title)
        else:
            pdf_writer(staging / "resume.pdf")
        if letter_text is not None:
            _write_text(staging / "letter.txt", letter_text)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target_version



def _changes(
    entries: Sequence[_Entry],
    selection: _Selection,
    excluded: int,
    cover_letter: bool,
    draft: CheckedDraft,
) -> list[str]:
    original = [entry.identifier for entry in entries]
    selected = [entry.identifier for entry in selection.entries]
    changes: list[str] = []
    if excluded:
        changes.append("excluded draft or unconfirmed resume content")
    if set(selected) != set(original):
        changes.append("selected confirmed resume entries for this posting")
    if selected != [identifier for identifier in original if identifier in set(selected)]:
        changes.append("reordered selected confirmed resume entries")
    changes.append("selected and ordered confirmed resume bullets")
    rewritten = sum(item["original"] != item["final"] for item in draft.provenance if item["kind"] == "resume_bullet")
    if rewritten:
        changes.append(f"rewrote {rewritten} resume bullets with original wording retained for comparison")
    if cover_letter:
        changes.append("drafted a personalized cover letter grounded in selected source facts")
    restored = sum(item["recovery"] == "restored_original" for item in draft.provenance)
    omitted = sum(item["recovery"] == "omitted_paragraph" for item in draft.provenance)
    if restored:
        changes.append(f"restored {restored} original resume bullets after unsupported or ambiguous rewrites")
    if omitted:
        changes.append(f"removed {omitted} unsupported or ambiguous cover-letter paragraphs")
    changes.append("a second provider pass checked each generated passage; its verdicts are retained")
    return changes


def prepare_application(
    posting: Mapping[str, object],
    profile: Profile,
    directory: Path | str,
    *,
    provider: str,
    cover_letter: bool = False,
    completion: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Prepare immutable, reviewable application documents.

    ``completion`` is a test seam with the same keyword contract as
    :func:`venator.llm.complete`. Fresh successful generation uses exactly two
    bounded calls through the same named provider: drafting, then factuality
    review. A complete cached version uses none. Deterministic and semantic
    checks restore original bullets or omit unsupported letter prose before
    replacing artifacts; malformed reviews leave previous artifacts intact.
    """

    if not isinstance(posting, Mapping):
        raise TypeError("posting must be a mapping")
    if not isinstance(provider, str) or provider.casefold() not in SUPPORTED_PROVIDERS:
        raise ValueError("provider must be one of claude, codex, api")
    provider = provider.casefold()
    directory = Path(directory)
    resume = _resume_mapping(profile)
    if not _confirmed(resume):
        raise ValueError("profile resume is draft or unconfirmed")
    name = _text(resume.get("name"))
    if not name or not name.strip():
        raise ValueError("profile resume needs a confirmed identity name")
    contact = _source_without_controls(resume.get("contact"))
    if not isinstance(contact, Mapping) or not any(text.strip() for text in _flatten_text(contact)):
        raise ValueError("profile resume needs confirmed contact facts")
    posting_version = posting_revision(posting)
    profile_version = filters_version(profile.constraints_path, profile.targeting_path)
    input_version = _input_version(profile, resume)
    existing = _usable_manifest(
        directory,
        _read_manifest(directory),
        provider=provider,
        posting_version=posting_version,
        profile_version=profile_version,
        input_version=input_version,
        cover_letter=cover_letter,
    )
    specs, entries, excluded = _catalogue(resume)
    if not entries:
        raise ValueError("profile resume has no confirmed entries to prepare")
    sources = _prompt_sources(entries)
    from venator.tailor.reuse import reuse_checked_draft
    if existing is not None and reuse_checked_draft(existing, sources, cover_letter=cover_letter) is not None:
        return existing
    # Catch unsupported original source glyphs before any provider spend.
    original_selection = _Selection(tuple(entries), {entry.identifier: entry.bullets for entry in entries})
    reference = profile.directory / "resume-reference"
    use_reference = reference.exists()
    if use_reference:
        from venator.resume.reference import preflight_reference
        preflight_reference(_structured_resume(resume, specs, original_selection), reference)
    else:
        _validate_fonts(_resume_lines(resume, specs, original_selection))
    draft = None
    previous = _read_manifest(directory)
    facts_version = _input_version(profile, resume, include_layout=False)
    if (use_reference and previous and previous.get("facts_version", previous.get("input_version")) == facts_version
            and previous.get("preparation_revision") in {4, 5, 6, 7, 8}):
        # Reformat only unchanged, hash-checked documents. The original review
        # remains tied to the same candidate facts and employer description.
        compatible = {**previous, "input_version": input_version, "preparation_revision": PREPARATION_REVISION}
        if _usable_manifest(directory, compatible, provider=provider, posting_version=posting_version,
                            profile_version=profile_version, input_version=input_version, cover_letter=cover_letter):
            draft = reuse_checked_draft(previous, sources, cover_letter=cover_letter)
    reused_writing = draft is not None
    if draft is None:
        draft = generate_and_check(_plain_copy(posting), sources, provider=provider,
                                   cover_letter=cover_letter, completion=completion)
    selection = _reference_selection(draft, entries) if use_reference else _selection(draft, entries)
    lines = _resume_lines(resume, specs, selection)
    resume_text = "\n".join(line.text for line in lines) + ("\n" if lines else "")
    letter_text = _cover_letter(name, draft) if cover_letter else None
    pdf_writer = None
    if use_reference:
        from venator.resume.reference import render_reference
        structured = _structured_resume(resume, specs, selection)
        preflight_reference(structured, reference)
        pdf_writer = lambda path: render_reference(structured, path, reference)

    version_payload = {
        "posting_version": posting_version,
        "profile_version": profile_version,
        "input_version": input_version,
        "facts_version": facts_version,
        "preparation_revision": PREPARATION_REVISION,
        "provider": provider,
        "cover_letter": cover_letter,
        "resume_text": resume_text,
        "letter_text": letter_text,
    }
    version = hashlib.sha256(
        json.dumps(version_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:20]
    directory.mkdir(parents=True, exist_ok=True)
    committed_version = _commit_version(
        directory,
        version,
        resume_text=resume_text,
        resume_lines=lines,
        letter_text=letter_text,
        title=_text(posting.get("title")) or "Prepared resume",
        pdf_writer=pdf_writer,
    )
    manifest: dict[str, object] = {
        "prepared": True,
        "version": committed_version,
        "provider": provider,
        "posting_version": posting_version,
        "profile_version": profile_version,
        "input_version": input_version,
        "preparation_revision": PREPARATION_REVISION,
        "file_hashes": _file_hashes(directory / "versions" / committed_version, cover_letter),
        "facts_version": facts_version,
        "resumeText": resume_text,
        "letterText": letter_text,
        "changes": _changes(entries, selection, excluded, cover_letter, draft),
        "draftProvenance": list(draft.provenance),
        "factualityReview": {"provider": provider, "checks": list(draft.review)},
        "layout": "uploaded_reference" if use_reference else "basic",
        "renderedResume": _structured_resume(resume, specs, selection),
        "draftSelection": {"entry_ids": list(draft.selected_entry_ids), "resume_bullets": list(draft.resume_bullets),
                           "letter_paragraphs": list(draft.letter_paragraphs)},
        "message": (
            "Drafts ready; rejected wording was restored from original resume facts or removed from the letter."
            if any(item["recovery"] for item in draft.provenance) else
            "Drafts ready after source-linked factuality checks; preview is optional."
        ),
    }
    if use_reference:
        manifest["changes"].insert(0, "preserved the uploaded resume's fonts, layout, section order, and entry structure")
    if reused_writing:
        manifest["changes"].append("reused previously checked writing without another AI request")
    _write_manifest(_manifest_path(directory), manifest)
    return manifest


__all__ = ["SELECTION_SCHEMA", "SUPPORTED_PROVIDERS", "prepare_application"]
