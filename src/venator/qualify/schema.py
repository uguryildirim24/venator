"""Frozen compiler facts and a separate, deliberately narrow student input type."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Mapping

Withheld = Literal["pronoun", "protected_term"] | None
FactStatus = Literal["confirmed", "draft"]
Visibility = Literal["visible", "withheld"]
DegreeLevel = Literal["high_school", "associate", "bachelor", "master", "doctorate", "unknown"]
EducationStatus = Literal["awarded", "in_progress", "unknown"]
ExperienceKind = Literal["employment", "academic_lab", "internship", "campus", "project", "unknown"]
NameMap = Mapping[str, str]


@dataclass(frozen=True)
class Interval:
    start: str
    end: str | None
    is_current: bool


@dataclass(frozen=True)
class Bullet:
    id: str
    source_path: str
    fact_status: FactStatus
    text: str
    withheld: Withheld


@dataclass(frozen=True)
class Education:
    id: str
    source_path: str
    fact_status: FactStatus
    degree_level: DegreeLevel
    field_of_study: str | None
    status: EducationStatus
    expected_completion: str | None
    awarded_on: str | None
    institution: str | None
    institution_placeholder: str
    gpa: str | None
    withheld: Withheld
    unknown_reason: str | None = None


@dataclass(frozen=True)
class Experience:
    id: str
    source_path: str
    fact_status: FactStatus
    kind: ExperienceKind
    role: str
    role_withheld: Withheld
    organization: str | None
    organization_placeholder: str
    interval: Interval | None
    months: int | None
    guaranteed_months: int | None
    current: bool
    bullets: tuple[Bullet, ...]
    unknown_reason: str | None = None


@dataclass(frozen=True)
class Skill:
    id: str
    source_path: str
    fact_status: FactStatus
    name: str
    evidence_level: Literal["listed", "bullet", "coursework"]
    supporting_ids: tuple[str, ...]
    withheld: Withheld


@dataclass(frozen=True)
class Coursework:
    id: str
    source_path: str
    fact_status: FactStatus
    name: str
    education_id: str | None
    withheld: Withheld


@dataclass(frozen=True)
class Credential:
    id: str
    source_path: str
    fact_status: FactStatus
    name: str
    withheld: Withheld


@dataclass(frozen=True)
class Authorization:
    id: Literal["auth"]
    source_path: str
    fact_status: FactStatus
    requires_sponsorship: bool | None
    authorized_to_work: bool | None
    withheld: Withheld = None


@dataclass(frozen=True)
class Availability:
    id: Literal["avail"]
    source_path: str
    fact_status: FactStatus
    available_from: str | None
    available_until: str | None
    expected_graduation: str | None
    expected_graduation_confirmed: bool
    enrollment: str | None
    reference_date: str | None
    withheld: Withheld = None


@dataclass(frozen=True)
class EducationPolicy:
    id: Literal["edupol"]
    source_path: str
    fact_status: FactStatus
    holds: str | None
    in_progress: str | None
    kill_completed_degree: bool
    withheld: Withheld = None


@dataclass(frozen=True)
class Fact:
    id: str
    kind: Literal["education", "experience", "bullet", "skill", "coursework", "credential", "auth", "avail", "edupol"]
    source_path: str
    fact_status: FactStatus
    withheld: Withheld
    text: str


@dataclass(frozen=True)
class CompiledProfile:
    schema: Literal["qualify-profile-3"]
    compiler_version: str
    profile_hash: str
    synthetic: bool
    education: tuple[Education, ...]
    experience: tuple[Experience, ...]
    skills: tuple[Skill, ...]
    coursework: tuple[Coursework, ...]
    credentials: tuple[Credential, ...]
    authorization: Authorization
    availability: Availability
    education_policy: EducationPolicy
    names: NameMap
    facts_by_id: Mapping[str, Fact]


@dataclass(frozen=True)
class Segment:
    seg_id: str
    months: int
    active: frozenset[str]
    contiguous_with_previous: bool


@dataclass(frozen=True)
class Evidence:
    segments: tuple[Segment, ...]
    months_by_entry: Mapping[str, tuple[int, int] | None]
    occupancy_all: int
    entries_unknown_interval: frozenset[str]
    entries_future: frozenset[str]
    cutoff_month: str
    cutoff_source: Literal["as_of", "reference_date"]
    experience_refutable: bool
    education_refutable: bool

    def guaranteed(self, ids: Iterable[str]) -> int:
        selected = frozenset(ids)
        return sum(segment.months for segment in self.segments if segment.active & selected)


@dataclass(frozen=True)
class E7:
    recency_clause: str | None = None
    gpa_clause: str | None = None


@dataclass(frozen=True)
class StudentEducation:
    id: str
    fact_status: FactStatus
    degree_level: DegreeLevel
    field_of_study: str | None
    status: EducationStatus
    expected_completion: str | None
    institution_placeholder: str
    award_year: str | None
    gpa: str | None


@dataclass(frozen=True)
class StudentBullet:
    id: str
    fact_status: FactStatus
    text: str


@dataclass(frozen=True)
class StudentExperience:
    id: str
    fact_status: FactStatus
    role: str
    kind: ExperienceKind
    months: int | None
    current: bool
    organization_placeholder: str
    bullets: tuple[StudentBullet, ...]


@dataclass(frozen=True)
class StudentNamedFact:
    id: str
    fact_status: FactStatus
    name: str
    evidence_level: Literal["listed", "bullet", "coursework"] | None = None
    supporting_ids: tuple[str, ...] = ()
    education_id: str | None = None


@dataclass(frozen=True)
class WithheldCounts:
    bullets: int
    names: int
    other: int


@dataclass(frozen=True)
class StudentAuthorization:
    id: Literal["auth"]
    fact_status: FactStatus
    requires_sponsorship: bool | None
    authorized_to_work: bool | None


@dataclass(frozen=True)
class StudentAvailability:
    id: Literal["avail"]
    fact_status: FactStatus
    available_from: str | None
    available_until: str | None
    expected_graduation: str | None
    expected_graduation_confirmed: bool
    enrollment: str | None


@dataclass(frozen=True)
class StudentEducationPolicy:
    id: Literal["edupol"]
    fact_status: FactStatus
    holds: str | None
    in_progress: str | None
    kill_completed_degree: bool


@dataclass(frozen=True)
class StudentProjection:
    """The only Profile data accepted by the downstream renderer."""

    schema: Literal["qualify-profile-3"]
    education: tuple[StudentEducation, ...]
    experience: tuple[StudentExperience, ...]
    skills: tuple[StudentNamedFact, ...]
    coursework: tuple[StudentNamedFact, ...]
    credentials: tuple[StudentNamedFact, ...]
    authorization: StudentAuthorization
    availability: StudentAvailability
    education_policy: StudentEducationPolicy
    withheld_counts: WithheldCounts
