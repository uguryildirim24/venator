"""The compact state used by the keep adapter, without Profile identity.

Serialization is part of the model input contract. Keep field order, whitespace
normalization, and the requirement-span selection identical to training.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping

from venator.profile import Profile
from venator.qualify.compile import compile_profile, project
from venator.qualify.engine import evidence
from venator.qualify.posting import canonical_posting
from venator.qualify.schema import CompiledProfile, E7

_CONTACT = re.compile(
    r'https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|'
    r'(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)',
    re.I,
)
_HEADING = re.compile(r'qualif|requir|skill|responsib|duties|about.*role|position|education|experience', re.I)
_REQUIREMENT = re.compile(
    r'\b(intern|co-op|sponsor|citizen|clearance|authorization|CPT|OPT|export|visa|'
    r'bachelor|master|PhD|degree|must|required|minimum|preferred)\b', re.I,
)


def serialize(state: Mapping[str, str]) -> bytes:
    return json.dumps(dict(state), ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def input_hash(state: Mapping[str, str]) -> str:
    return hashlib.sha256(serialize(state)).hexdigest()


class CompactStateBuilder:
    def __init__(self, profile: Profile, *, as_of_month: str,
                 compiled_profile: CompiledProfile | None = None):
        self.profile = profile
        self.month = as_of_month
        self.compiled = compiled_profile or compile_profile(profile, as_of_month=as_of_month)
        name = profile.resume.get('name', '')
        contact = profile.resume.get('contact', {})
        values = list(contact.values()) if isinstance(contact, Mapping) else []
        values.append(name)
        if isinstance(name, str):
            values.extend(word for word in re.split(r'\s+', name) if len(word) >= 3)
        self.identity = sorted((s for s in values if isinstance(s, str) and s), key=len, reverse=True)

    def clean(self, value: object) -> str:
        text = _CONTACT.sub('[contact removed]', str(value))
        for term in self.identity:
            text = re.sub(re.escape(term), '[identity removed]', text, flags=re.I)
        return re.sub(r'\s+', ' ', text).strip()

    def build(self, posting: Mapping[str, object]) -> dict[str, str]:
        canonical = canonical_posting(posting, {})
        engine = evidence(self.compiled, canonical, self.month)
        projection, _ = project(self.compiled, E7(engine.recency_clause, engine.gpa_clause))
        parts: list[str] = []
        for education in projection.education:
            if education.fact_status == 'confirmed':
                parts.append('Education: ' + '; '.join(str(v) for v in (
                    education.degree_level, education.field_of_study, education.status,
                    education.expected_completion, education.award_year, education.gpa,
                ) if v is not None))
        for experience in projection.experience:
            if experience.fact_status == 'confirmed' and experience.role != '[withheld]':
                parts.append(
                    f'Experience: {experience.role}; {experience.kind}; {experience.months} months; '
                    f'current={experience.current}. ' + ' '.join(
                        bullet.text for bullet in experience.bullets if bullet.fact_status == 'confirmed'
                    )
                )
        for field in ('skills', 'coursework', 'credentials'):
            parts.append(field + ': ' + '; '.join(
                fact.name for fact in getattr(projection, field)
                if fact.fact_status == 'confirmed' and fact.name != '[withheld]'
            ))
        parts.append(
            f'Expected graduation: {self.compiled.availability.expected_graduation}; '
            f'total non-overlapping experience: {engine.occupancy_all} months.'
        )
        policy = self.profile.filters.compact_policy
        policy_fields = (dataclasses.asdict(policy) if policy is not None else {
            'restricted_roles': 'review', 'temporary_student_authorization_exclusion': 'review',
            'no_sponsorship_student': 'review', 'no_sponsorship_nonstudent': 'review',
            'unmet_completed_degree': 'review', 'domains': (
                'biochemistry', 'laboratory_research', 'pharmacy', 'quality_control',
                'biomanufacturing', 'clinical_research', 'computational_life_sciences', 'regulatory_science',
            ),
        })
        parts.append('Policy: ' + json.dumps(
            {k: v for k, v in policy_fields.items() if k != 'policy_version'},
            separators=(',', ':'),
        ))
        chosen = [span.text for span in canonical.spans if (
            span.atoms or span.modality_hint != 'unknown'
            or (span.heading and _HEADING.search(span.heading))
            or _REQUIREMENT.search(span.text)
        )]
        texts = list(dict.fromkeys([canonical.description_text[:900], *chosen]))
        return {
            'title': self.clean(canonical.title),
            'employer': self.clean(posting.get('company') or ''),
            'requirements_and_qualifications': self.clean('\n'.join(texts)),
            'confirmed_profile': self.clean('\n'.join(parts)),
        }
