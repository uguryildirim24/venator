"""Dataclass schema for a Profile: the facts and preferences one person owns.

A Profile is a directory of three YAML files — ``resume.yaml`` (canonical
facts), ``constraints.yaml`` (Hard Filter policy and application answers), and
``targeting.yaml`` (what to look for and where to look) — loaded
into the frozen dataclasses below. Every field is optional at the file level:
an empty Profile loads and every stage degrades to "nothing configured, so
nothing is killed", never to a crash. The one exception is deliberate: writing
a ``filters:`` policy without an ``enabled:`` list is refused rather than
quietly running no filter at all, because that failure passes every Posting.

**Terms are words; ``*_patterns`` are regular expressions.** ``terms``,
``places``, ``region`` and ``cities`` are escaped and matched literally, so a
place name may hold any punctuation. Every field whose name ends in
``_patterns`` is compiled verbatim and is therefore code: an entry like
``(a+)+$`` backtracks catastrophically and will hang the Hard Filter pass on
some Posting instead of failing at load time. ``venator.match.run`` bounds each
Posting with a wall-clock budget and names the Profile when it expires, but the
budget is a backstop — keep patterns simple, anchor them, and prefer ``terms``
whenever the wording is literal.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from datetime import date
from typing import Mapping


def new_profile_identifier() -> str:
    """Mint the opaque identifier a new Profile is stamped with, once, at setup.

    Written into ``targeting.yaml: profile.id`` when a scaffold is copied, and
    never regenerated afterwards — the value's whole purpose is to stay the same
    while everything around it moves. Random (uuid4) rather than derived from
    anything: see ``Profile.identifier`` for why it must not be personal.
    """
    return uuid.uuid4().hex


REMOTE_PREFERENCES = ("required", "preferred", "acceptable", "no")

SHIFT_PREFERENCES = ("no_preference", "weekdays_only", "no_nights", "no_weekends")
"""Closed values for ``FilterPolicy.shift_preference``.

Loaded from ``targeting.yaml: filters.shift_preference``. Default
``no_preference``. R3 owns the deterministic Hard Filter that reads this
field; this module only defines and loads it.
"""

JEV_EXCLUDE_OR_REVIEW = ("exclude", "review")
JEV_RETAIN_OR_REVIEW = ("retain", "review")
JEV_DOMAIN_TOKENS = (
    "biochemistry",
    "laboratory_research",
    "pharmacy",
    "quality_control",
    "biomanufacturing",
    "clinical_research",
    "computational_life_sciences",
    "regulatory_science",
)
JEV_POLICY_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True)
class Matcher:
    """One named group of wording, compiled from terms or patterns."""

    label: str
    pattern: re.Pattern[str] | None = None

    def search(self, text: str) -> re.Match[str] | None:
        return self.pattern.search(text) if self.pattern is not None else None

    def fullmatch(self, text: str) -> re.Match[str] | None:
        return self.pattern.fullmatch(text) if self.pattern is not None else None

    @property
    def configured(self) -> bool:
        return self.pattern is not None


@dataclass(frozen=True)
class TimingPolicy:
    """Explicit availability facts used when a Posting states a time.

    Dates are optional by design.  A missing Posting start or cohort is an
    unknown fact, not a date inferred from when the row was discovered.  The
    loader accepts these fields in ``targeting.yaml: search.timing``. ``allowed_years`` and
    ``allowed_seasons`` retain year/season precision when an exact day is not
    confirmed.
    """

    available_from: date | None = None
    available_until: date | None = None
    allowed_months: tuple[int, ...] = ()
    allowed_seasons: tuple[str, ...] = ()
    allowed_years: tuple[int, ...] = ()
    expected_graduation: date | None = None
    expected_graduation_confirmed: bool = False
    # Resume/Profile data often confirms only a graduation month.  Retain that
    # precision so a role beginning in the same month stays unknown rather than
    # being compared against an invented day.
    expected_graduation_precision: str = "day"
    enrollment: str | None = None
    reference_date: date | None = None

    @property
    def configured(self) -> bool:
        return any(
            (
                self.available_from,
                self.available_until,
                self.allowed_months,
                self.allowed_seasons,
                self.allowed_years,
                self.expected_graduation and self.expected_graduation_confirmed,
                self.enrollment,
            )
        )


@dataclass(frozen=True)
class WorkAuthorizationPolicy:
    """Posting wording that makes the Owner ineligible outright."""

    restrictions: tuple[re.Pattern[str], ...] = ()
    #: Wording the Profile has decided not to kill on but wants said out loud —
    #: "we are unable to sponsor" is a deliberate pass, not an absence of
    #: restrictions, and a Filter Decision that reports it as the latter reads
    #: downstream as a clean bill of health.
    noted: tuple[re.Pattern[str], ...] = ()
    noted_label: str = "wording the Profile passes deliberately"
    pass_reason: str = "no explicit work-authorization restriction"

    @property
    def configured(self) -> bool:
        return bool(self.restrictions)


#: The qualifications a Profile can name, lowest first. This ladder is code
#: rather than Profile data because the wording that identifies a degree in a
#: Posting is code too (``_DEGREE_PATTERN`` in ``venator.match.filters``): a
#: Profile naming a rung the matcher can never read would be a rung that decides
#: nothing. It is deliberately short — these are the rungs the matcher can tell
#: apart, and ``high_school`` is the floor an Owner holding no post-secondary
#: qualification names.
DEGREE_LEVELS: tuple[str, ...] = ("high_school", "associate", "bachelor", "master", "doctorate")


@dataclass(frozen=True)
class EducationFitPolicy:
    """How much completed education and industry experience disqualifies.

    Two ways to say what the Owner has, and a Profile picks exactly one.

    ``kill_completed_degree`` is the original yes/no, and it still means what it
    has always meant: the Owner holds no completed degree, so any requirement for
    one is out of reach. It is a student's setting, and it cannot say anything
    else — which is why it is not reinterpreted here. A Profile that sets it keeps
    its verdicts unchanged.

    ``holds`` and ``in_progress`` are the widened model: the highest qualification
    the Owner has completed, and one they are working toward. A requirement at or
    below ``holds`` is met; one above ``in_progress`` is out of reach; anything
    between the two, and any requirement whose level cannot be read from the
    Posting's own words, resolves to "not sure" and reaches assessment and Jev.
    An unresolved requirement is never a confident kill.
    """

    kill_completed_degree: bool = False
    #: Explicit present student category; an expected graduation alone does not set it.
    current_student_level: str | None = None
    #: The highest qualification the Owner holds, named from ``DEGREE_LEVELS``.
    #: ``None`` is "not configured", not "holds nothing".
    holds: str | None = None
    #: A qualification the Owner is working toward and does not yet hold.
    in_progress: str | None = None
    experience_years_kill: int | None = None
    experience_years_implausible: int = 15

    @property
    def attainment_configured(self) -> bool:
        """Whether the Profile states what the Owner has, in the widened model."""
        return self.holds is not None or self.in_progress is not None

    @property
    def reads_degrees(self) -> bool:
        """Whether a degree requirement means anything to this Profile at all."""
        return self.kill_completed_degree or self.attainment_configured

    @property
    def configured(self) -> bool:
        return self.reads_degrees or self.experience_years_kill is not None


@dataclass(frozen=True)
class RoleLevel:
    """One rung of a Profile's seniority ladder, named by the Profile."""

    name: str
    titles: Matcher = field(default_factory=lambda: Matcher("titles"))


#: Punctuation that offers two titles rather than qualifying one. "Engineer II /
#: Senior Software Engineer" is one requisition open at two rungs; "Associate
#: Director" is a single role whose words compose into one name.
#:
#: The separator is captured so ``re.split`` hands it back with the fragments.
#: A fragment that is not an offer of its own is re-joined to the previous one
#: through the punctuation the Owner's employer actually wrote, so every offer
#: ``alternatives`` returns is a span of the title rather than a phrase this code
#: assembled. Joining with a space instead synthesised ``'Loading Dock'`` out of
#: ``'Loading / Dock'`` — and ``exclude``, which is word-bounded and literal,
#: then killed a Posting on a phrase its title never contained.
_STATED_ALTERNATIVE = re.compile(r"(\s*/\s*|\s+\bor\b\s+)", re.IGNORECASE)
#: Where a title stops naming the role and starts naming its context. Everything
#: after the first comma, bracket or dash is the department, the team, the
#: product, the city or the shift — never the rung — and a slash in *there*
#: separates technologies, not rungs: "Product Lead, Software/Applied AI" is one
#: Product Lead.
#:
#: The whitespace ahead of the boundary belongs to the context, so the leading
#: segment comes back trimmed as the Owner wrote it.
_TITLE_QUALIFIER = re.compile(r"\s*[,(\[]|\s+[-–—]")
_WORD = re.compile(r"[A-Za-z][A-Za-z.'’-]*")


def _looks_like_a_title(fragment: str) -> bool:
    """Whether a fragment naming no rung is still credible as a title of its own.

    This decides whether an unreadable fragment means "a rung the ladder cannot
    read" — which passes — or is just debris from a slash that was
    never an alternation. "Engineer II" is a title; the "II" of "Engineer I/II"
    and the "AWS" of "Technical Lead / AWS / SaaS" are not.
    """
    words = _WORD.findall(fragment)
    if not any(len(word) >= 3 for word in words):
        return False
    return len(words) > 1 or max(len(word) for word in words) >= 8


@dataclass(frozen=True)
class RoleTargetPolicy:
    """The band of the seniority ladder the Owner wants, and functions they do not.

    This is a preference on a scale, not a test for one kind of person: the
    Profile writes the ladder and names the rungs it accepts, so a student
    targeting ``[intern, entry]`` and a mid-career marketer targeting
    ``[entry, mid]`` are the same rule with different data. A title that names no
    rung at all is left unplaced and passes — the filter cuts what a Profile has
    clearly said it does not want, and leaves everything else to assessment.

    Everywhere the rung is ambiguous, the reading *nearest the accepted band*
    wins. Across the titles a Posting offers — "Engineer II / Senior Software
    Engineer" — the offer closest to the band decides, because the Posting is
    open at both. Within one offered title the same rule holds: "Senior Research
    Associate" is read as an Associate by a Profile accepting entry work and as
    a Senior by one accepting senior work, and either way it reaches
    assessment and Jev rather than dying on the half the Owner is not targeting.
    Both follow from the one rule this filter cannot break — an unresolved title
    resolves to "not sure" and reaches assessment and Jev, never to "no".

    Resolving toward the *bottom of the ladder* instead is the same rule only
    for a band anchored there, and is maximally aggressive for every other band
    shape: with ``accept: [mid, senior]`` on this Owner's ladder, "Senior
    Research Associate", "Lead Technician, Cell Culture" and "Staff Coordinator"
    all die as entry work "below the band". A senior title can contain a junior rung word and be killed anyway. Every rung word a Profile writes at the bottom
    of its ladder — ``coordinator``, ``associate``, ``specialist`` — becomes a
    killer of the senior titles that contain it, and
    ``profiles/example/targeting.yaml`` already ships that band shape.
    ``tests/match/test_filters.py`` holds both shapes against the same titles,
    and fuzzes the property, so the asymmetry cannot come back unnoticed.
    """

    label: str = "role target"
    levels: tuple[RoleLevel, ...] = ()
    accept: tuple[str, ...] = ()
    exclude: Matcher = field(default_factory=lambda: Matcher("an excluded function"))

    @property
    def configured(self) -> bool:
        return bool(self.accept and self.levels) or self.exclude.configured

    @property
    def band(self) -> tuple[int, int] | None:
        """The lowest and highest accepted rung, or ``None`` when none is accepted.

        ``None`` is the whole point of the return type. This used to fall back to
        ``(0, 0)``, which does not mean "no band" — it means "this Profile accepts
        exactly the bottom rung of its ladder", the single most aggressive band a
        ladder can express. A Profile that wrote ``levels:`` and left ``accept:``
        empty then killed every Posting above rung 0 confidently, naming a band
        that did not exist: "above the  seniority band the Profile targets". The
        loader now refuses that Profile outright, and this returns ``None`` so the
        filter abstains even for one nobody validated.
        """
        indices = [index for index, level in enumerate(self.levels) if level.name in self.accept]
        return (min(indices), max(indices)) if indices else None

    def alternatives(self, title: str) -> tuple[str, ...]:
        """The roles a Posting's title offers — the leading segment only.

        **The one place a title becomes a role.** Every check that reads a title
        for the job it names goes through this: the rung ladder and the excluded
        functions both read what it returns, and a check added later gets the
        right reading by default rather than by its author remembering. Reading
        the title as written is a different question — identity, display, the
        text a person is shown — and the callers that want it ask for the title
        rather than for this.

        That is not a style preference. The same misreading has now been found
        twice in one function: the ladder scanned "Research Associate, Head and
        Neck Oncology" for a rung across an anatomical site, and ``exclude``
        killed "Marketing Coordinator, Recruiter Enablement" on the name of the
        team it supports. Two branches of one filter, the same defect, fixed
        once each. The third would have been somebody else's.

        A title is a role followed by its context: "Research Associate, Head and
        Neck Oncology" is an Associate working on head and neck cancers, and
        "Clinical Research Coordinator, CTO" is a Coordinator in a Clinical
        Trials Office. Everything from the first comma, bracket or dash onward
        names a department, a product, a site or a shift, so the ladder never
        reads it: a department that happens to share a word with a rung —
        ``head``, ``officer``, ``cto`` — would otherwise place the Posting at a
        rung nobody offered. Only the leading segment is split into offers, and
        only on a fragment that either places on this ladder or reads as a title
        on its own. Both restrictions are earned: without the first, "Strategic
        Commercial Director - Pharma/Life Sciences" offers a rung called "Life
        Sciences"; without the second, "Senior Engineer I/II" offers one called
        "II".

        The boundary is the first one with a role in front of it. A title that
        *opens* with a bracket — "(Senior) Director, Portfolio Strategy",
        "[Remote] Principal Software Developer" — is prefixing the role, not
        following it, and taking that boundary would leave the leading segment
        empty.

        This guard is taste, not a rescue, and the claim that it saved three
        corpus Postings from passing was wrong. ``tuple(stated) or
        (title,)`` below already falls back to the whole title, so an empty
        segment costs a reading of the context rather than a reading of nothing:
        replaying the corpus without this guard moves 0 verdicts and 0 reasons,
        and all three bracket-opening titles kill either way. What it earns is
        that ``alternatives`` returns a role — "(Senior) Director" — rather than
        a whole title with its department attached, which is what the rest of
        this class is for.

        Every offer returned is a span of the title as written. A fragment that
        is not an offer of its own is re-joined through the separator that stood
        between them, never through a space this code chose: merging with a space
        turned "Loading / Dock" into the offer "Loading Dock", and ``exclude``
        matches its terms literally between word boundaries, so a Profile
        excluding ``loading dock`` killed a title that never contained the
        phrase. That branch is the one place in this filter with no conservative
        escape — it runs first and returns a kill directly — so it must never be
        handed a phrase the employer did not write. Avoid synthesized exclusion terms: the corpus only holds punctuation that happened.
        """
        head = title
        for found in _TITLE_QUALIFIER.finditer(title):
            if _WORD.search(title[: found.start()]):
                head = title[: found.start()]
                break
        stated: list[str] = []
        # ``split`` on a capturing pattern interleaves the separators, so the
        # fragments sit at the even indices and each one's preceding separator at
        # the odd index before it. ``crossed`` accumulates every separator — and
        # every empty fragment between two of them — passed since the last offer
        # was kept, so re-joining through it puts back exactly the text that
        # stood between the two offers. Re-joining through the *last* separator
        # alone is not enough: "Loading / / Dock" would drop the middle one and
        # merge to "Loading / Dock", which is again a phrase the employer did not
        # write. Doubled punctuation is rarer than a single slash, but "rarer" is
        # what this whole branch already got wrong once.
        parts = _STATED_ALTERNATIVE.split(head)
        crossed = ""
        for index in range(0, len(parts), 2):
            if index:
                crossed += parts[index - 1]
            fragment = parts[index].strip()
            if not fragment:
                crossed += parts[index]
                continue
            if stated and self.place(fragment) is None and not _looks_like_a_title(fragment):
                # ``stated`` is non-empty only after a fragment was appended, so
                # ``index`` is at least 2 here and the text before it exists.
                stated[-1] = f"{stated[-1]}{crossed}{fragment}"
                crossed = ""
                continue
            stated.append(fragment)
            crossed = ""
        return tuple(stated) or (title,)

    def distance_from_band(self, index: int) -> int:
        """How far a rung sits outside the accepted band, in rungs. Zero inside it.

        One definition, read by ``place`` here and by ``role_target_filter`` when
        it picks which offer to kill on, so the two cannot drift apart on what
        "nearest the band" means.

        With no band at all, every rung is zero away from it: there is no band to
        be outside of, so nothing can be ranked as further from one. That keeps
        ``namings`` ordering by rung index alone — which is all ``alternatives``
        needs from ``place`` — and it cannot become a kill, because
        ``role_target_filter`` abstains before it reaches the ladder.
        """
        band = self.band
        if band is None:
            return 0
        lowest, highest = band
        if index < lowest:
            return lowest - index
        return index - highest if index > highest else 0

    def namings(self, title: str) -> tuple[tuple[int, RoleLevel, re.Match[str]], ...]:
        """Every rung one stated title names, the reading nearest the band first.

        Order is ``(distance from the band, rung index)``: a rung inside the band
        beats every rung outside it, a nearer outside rung beats a further one,
        and a tie between two equally distant readings goes to the junior of them
        — the direction the governing rule already prefers, and one that decides
        only the wording of a kill both readings agree on.
        """
        found = [
            (index, level, match)
            for index, level in enumerate(self.levels)
            if (match := level.titles.search(title)) is not None
        ]
        found.sort(key=lambda reading: (self.distance_from_band(reading[0]), reading[0]))
        return tuple(found)

    def place(self, title: str) -> tuple[int, RoleLevel, re.Match[str]] | None:
        """The rung one stated title names, resolved *toward the accepted band*.

        A title naming two rungs at once — "Senior Research Associate",
        "Associate Director" — is genuinely ambiguous about which one it is
        offering, and the governing rule settles ambiguity in one direction: to
        the reading the Owner might want, so assessment gets the call. A wrong
        kill costs the Owner a Posting they never learn existed; a wrong pass
        costs one more Posting to review.

        Resolving *down* the ladder is that rule only for a Profile whose band
        is anchored at the bottom, where nothing can be pulled below it. For any
        other band shape it is the opposite of the rule: with ``accept: [mid,
        senior]``, pulling "Senior Research Associate" down to Associate puts it
        below the band and kills it on the half the Owner is not targeting.
        Resolving toward the band reduces to the junior reading exactly when the
        band starts at rung 0 — which is why this change moves no decision for a
        bottom-anchored Profile — and stays conservative for every other shape.

        The ladder's order *is* the ladder: rung index is seniority, because the
        Profile writes ``levels`` lowest rung first. Nothing validates that, and
        nothing can — there is no seniority scale outside the one the Profile
        declares, so a ladder written upside down is a Profile that means the
        opposite, not a file this code can catch.
        """
        namings = self.namings(title)
        if not namings:
            return None

        # A single offered title is a composition, not a vote between words.
        # ``Associate Director`` and ``Senior Associate`` name the senior role
        # their complete phrase describes; choosing the junior token merely
        # because the Profile accepts it turns a director into entry work. True
        # alternatives have already been split by ``alternatives`` and therefore
        # never reach this branch with a slash/or between them.
        if len(namings) > 1:
            ordered = sorted(namings, key=lambda reading: reading[2].start())
            between = title[ordered[0][2].end() : ordered[-1][2].start()]
            if not re.search(r"[,/]|\bor\b", between, re.IGNORECASE):
                # “Executive Assistant” is a well-known role name whose first
                # word modifies the assistant role rather than promoting it to
                # the executive rung. Keep this narrow modifier exception
                # generic by consulting the matched terms, never a Profile or
                # person's title list.
                words = {match.group(0).casefold() for _, _, match in namings}
                if "executive" in words and "assistant" in words:
                    return min(namings, key=lambda reading: reading[0])
                return max(namings, key=lambda reading: reading[0])
        return namings[0]


@dataclass(frozen=True)
class SearchTargeting:
    """What the aggregator asks for, and the shape of role the Owner wants."""

    queries: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    seniority: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    remote: str | None = None
    salary_floor: int | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    timing: TimingPolicy = field(default_factory=TimingPolicy)

    @property
    def eligibility_configured(self) -> bool:
        """Whether explicit search preferences need deterministic checking."""
        return self.salary_floor is not None or self.timing.configured


@dataclass(frozen=True)
class JevPolicy:
    """Typed Jev search policy from ``targeting.yaml: filters.jev``.

    A missing block is ``None`` on ``FilterPolicy`` and does not invent
    another Owner's preferences. Every field is required when the block is
    present. ``policy_version`` is a local audit label; it never reaches
    wire state. The example policy uses ``review`` for
    ``no_sponsorship_nonstudent``, not ``exclude``.
    """

    policy_version: str
    restricted_roles: str
    temporary_student_authorization_exclusion: str
    no_sponsorship_student: str
    no_sponsorship_nonstudent: str
    unmet_completed_degree: str
    domains: tuple[str, ...]


@dataclass(frozen=True)
class FilterPolicy:
    """Everything the Hard Filters read. An unconfigured policy kills nothing."""

    enabled: tuple[str, ...] = ()
    qualification_mode: str = "deterministic"
    work_authorization: WorkAuthorizationPolicy = field(default_factory=WorkAuthorizationPolicy)
    education_fit: EducationFitPolicy = field(default_factory=EducationFitPolicy)
    role_target: RoleTargetPolicy = field(default_factory=RoleTargetPolicy)
    # Search preferences historically lived only on Profile.search. Keeping a
    # frozen copy here makes ``apply_filters(posting, profile.filters)`` observe
    # the same policy without changing its public call shape.
    search: SearchTargeting = field(default_factory=SearchTargeting)
    jev: JevPolicy | None = None
    shift_preference: str = "no_preference"


@dataclass(frozen=True)
class BoardRegistry:
    """The Sources to poll: ATS board tokens and their employer display names."""

    names: Mapping[str, str] = field(default_factory=dict)
    boards: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen=True freezes the fields, not the dicts behind them, and both of
        # these are handed out to the view builder and the discover adapters.
        object.__setattr__(self, "names", MappingProxyType(dict(self.names)))
        object.__setattr__(self, "boards", MappingProxyType(dict(self.boards)))

    @property
    def configured(self) -> bool:
        return any(tokens for tokens in self.boards.values())


@dataclass(frozen=True)
class Profile:
    """One person's Profile: the loaded files plus everything derived from them."""

    name: str
    directory: Path
    #: The opaque identifier stamped on every Filter Decision this Profile
    #: produces, from ``targeting.yaml: profile.id``. Empty here; ``identifier``
    #: is what callers read, because it carries the fallback.
    id: str = ""
    scaffold: bool = False
    resume: Mapping[str, object] = field(default_factory=dict)
    constraints: Mapping[str, object] = field(default_factory=dict)
    targeting: Mapping[str, object] = field(default_factory=dict)
    search: SearchTargeting = field(default_factory=SearchTargeting)
    sources: BoardRegistry = field(default_factory=BoardRegistry)
    filters: FilterPolicy = field(default_factory=FilterPolicy)

    @property
    def identifier(self) -> str:
        """What a Filter Decision records as the Profile that produced it.

        ``profile.id`` when the Profile declares one, and the Profile's
        ``name`` — which is itself the directory name unless overridden — when
        it does not. Three properties this value has to have, and one it
        deliberately does not:

        **Opaque, not the directory name.** The name is a human label and
        changes freely: someone renames ``profiles/example`` and every row already
        written still has to belong to them. ADR-0002 sharpens this — the
        tenant boundary is now the Install's *data directory*, which can be
        copied, synced and restored away from any checkout, so a stamp that
        only means something relative to a directory listing means nothing once
        the two are apart. A minted ``profile.id`` survives the rename; the
        directory name stays free to be edited.

        **Not personal.** ``data/`` is committed in this repository today, and a
        shipped Install's data directory gets synced and backed up. A name or an
        email in every row would put a person's identity in all of that for no
        gain, so the minted value is random (``new_profile_identifier``) and
        derived from nothing.

        **A Profile within one Install — never a person across Installs.** This
        is the closest thing the row schema has to the user column ADR-0002
        rules out, and it is not one: two Installs never share a store, so this
        value is never compared across them and identifies nobody. It
        distinguishes *this person's* several Profiles from each other, which is
        exactly what ``--profile`` exists for.

        **It is not an enforcement mechanism.** ``data/decisions/.profile``
        remains the lock that stops two Profiles interleaving; this is the
        evidence that makes a mis-pointed ``--decisions-dir`` detectable rather
        than trusted, and it is what makes a row self-describing once the data
        directory outlives the checkout that wrote it.

        The fallback is what keeps the whole thing additive: a Profile from
        before this field existed identifies as its name, which is exactly what
        the ``.profile`` claim already recorded, so no existing Install needs a
        flag day and no committed row needs rewriting (ADR-0001).

        Never empty: a Profile that explicitly writes ``name: ""`` still
        identifies as its directory, because a blank stamp would read as
        "unknown" and quietly match everything it was meant to distinguish.
        """
        return self.id or self.name or self.directory.name

    def __post_init__(self) -> None:
        # The three parsed files are handed to the renderer and the field
        # mapper; a frozen Profile that lets them be edited in place is not one.
        for name in ("resume", "constraints", "targeting"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @property
    def resume_path(self) -> Path:
        return self.directory / "resume.yaml"

    @property
    def constraints_path(self) -> Path:
        return self.directory / "constraints.yaml"

    @property
    def targeting_path(self) -> Path:
        return self.directory / "targeting.yaml"
