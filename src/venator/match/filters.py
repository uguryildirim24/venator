"""Conservative Hard Filters driven by a Profile's targeting policy.

Every individual rule returns a ``Reading`` — ``(verdict, rule, reason, fact)``,
where the first three are the Filter Decision and ``fact`` is a short token
naming what the rule read on a pass.  Ambiguous wording passes through to Jev
because a false kill is more costly than an extra Posting to qualify — which makes the pass the common outcome, and made the pass
the outcome the store could not explain.  A kill names its evidence in the
reason; a pass said only "passed", so a rule that was wrong in the passing
direction left nothing behind to count.  The ``fact`` is that record, and it is
recording only: it is read by nobody who decides anything, and it is deliberately
outside ``filters_version`` for that reason (``venator.match.store``).

The ``*_filter`` functions at the bottom of this module are each rule's
three-value view, unchanged, for callers that want the decision and not the
reading.

The rules here are generic; the wording they match on is Profile data
(``profiles/<name>/targeting.yaml``, ``filters:``).  A filter given no policy
kills nothing — an unconfigured Profile runs the whole pipeline and still
records a Filter Decision for every Posting.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Literal, NamedTuple, TypeAlias

from venator.match.timing import eligibility_finding, eligibility_relevant
from venator.profile.schema import (
    DEGREE_LEVELS,
    EducationFitPolicy,
    FilterPolicy,
    RoleTargetPolicy,
)
from venator.qualify.jev_contract import JevContext

Verdict: TypeAlias = Literal["pass", "kill"]
FilterResult: TypeAlias = tuple[Verdict, str | None, str]


class Reading(NamedTuple):
    """What one Hard Filter decided, and the fact it decided it on.

    ``verdict``, ``rule`` and ``reason`` are the Filter Decision exactly as they
    have always been. ``fact`` is new and is **recording, never deciding**: a
    short token naming what the rule read, present on a pass and ``None`` on a
    kill, where the reason already names the evidence.

    A kill says why. A pass said nothing but "passed", so a rule that was wrong
    in the passing direction — one that placed no rung it could read, one that
    saw a restriction and waived it — left no trace, and every audit of this
    corpus so far has had to reconstruct that reasoning by reading the code. The
    token is what makes it countable instead. It is a token rather than prose
    because prose cannot be compared across rows.

    The tokens are the rule's own vocabulary, not the Profile's: the Profile's
    labels are its wording for the same reading and are one lookup away, and the
    location string and title the reading came from are already on the Posting.
    Recording the reading rather than the text it read is what keeps this cheap
    enough to append to every pass in an append-only store that ships in the
    repository (docs/adr/0001-git-as-transport.md).

    The one exception is ``role_target``, whose token on a placed title *is* the
    rung's name, because the rung is the fact. ``unplaced`` and ``unconfigured``
    are therefore reserved: a Profile that named a rung either of those would be
    indistinguishable from a Posting the ladder declined to place. No committed
    Profile does, and a test pins that rather than leaving it assumed.
    """

    verdict: Verdict
    rule: str | None
    reason: str
    fact: str | None = None

    @property
    def decision(self) -> FilterResult:
        """The three values a Filter Decision has always carried, and only those."""
        return self.verdict, self.rule, self.reason


class Decision(NamedTuple):
    """Every Hard Filter's answer folded into the one decision the store records.

    ``facts`` maps rule name to the token that rule derived, for every rule that
    ran and passed. On a kill it is empty: ``apply_filters`` returns at the first
    kill, so the rules after it never ran, and the reason already says what the
    rule that fired read. On a pass every enabled rule is in it, and a Profile
    with no Hard Filter passes with an empty mapping — which is a fact of its own
    and is recorded as one, so that an *absent* ``facts`` in the store can keep
    meaning "this row predates the field" and nothing else.
    """

    verdict: Verdict
    rule: str | None
    reason: str
    facts: Mapping[str, str] = MappingProxyType({})


HardFilter: TypeAlias = Callable[[dict, FilterPolicy], Reading]

EMPTY_POLICY = FilterPolicy()

_BLOCK_TAGS = {
    "br",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "ol",
    "p",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}


class _TextExtractor(HTMLParser):
    def __init__(self, separator: str = " ") -> None:
        super().__init__(convert_charrefs=True)
        self.separator = separator
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append(self.separator)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
            return
        if self.ignored_depth:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append(self.separator)

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def strip_html(value: str, *, preserve_blocks: bool = False) -> str:
    """Decode ATS-escaped markup, remove tags, and normalize whitespace."""
    parser = _TextExtractor("\n" if preserve_blocks else " ")
    parser.feed(html.unescape(value or ""))
    parser.close()
    if preserve_blocks:
        return "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())
    return " ".join("".join(parser.parts).split())


def _evidence(text: str, match: re.Match[str], radius: int = 70) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    excerpt = " ".join(text[start:end].split())
    if start:
        excerpt = f"…{excerpt}"
    if end < len(text):
        excerpt = f"{excerpt}…"
    return excerpt


#: Filters that read the description and nothing else. On a Posting whose stored
#: description is a truncated snippet these cannot reach the requirements section,
#: so their own silence is ignorance rather than a finding.
DESCRIPTION_ONLY_RULES = ("work_authorization", "education_fit")

TRUNCATED_NOTE = "the stored description is a truncated snippet"


def _truncated(posting: dict) -> bool:
    """Whether Discover marked this Posting's description as an excerpt.

    Historical aggregator rows can hold a lede, not a Posting. They record
    that with ``snippet: true`` precisely so matching does not read missing detail
    as evidence that a requirement is absent — and every filter below that reads
    only the description asks before concluding anything from its own silence.
    """
    kind = str(posting.get("description_kind") or "").strip().casefold()
    return bool(posting.get("snippet")) or kind in {"snippet", "excerpt", "summary", "truncated"}


def _unassessed(rule: str) -> str:
    """A pass that declines to conclude, worded so it cannot read as a finding."""
    return f"{rule.replace('_', ' ')} could not be assessed: {TRUNCATED_NOTE}"


def _years(count: int) -> str:
    """A year count the way the Owner reads it. One year is not "1 years"."""
    return f"{count} year" if count == 1 else f"{count} years"


def _article(word: str) -> str:
    """The indefinite article for a rung the Profile named: "a executive" is not English."""
    return "an" if word[:1].lower() in "aeiou" else "a"


# Requirement-versus-preference wording. Two filters read it, so it lives above
# both rather than inside either.
_PREFERENCE = re.compile(r"\b(?:preferred|ideally|bonus|nice to have|a plus)\b", re.IGNORECASE)
_REQUIREMENT = re.compile(
    r"\b(?:required|requires?|requirements?|qualifications?|must|minimum|completed|completion|"
    r"earned|possess(?:es)?|hold(?:s)?|you have|about you|your background|"
    r"education and experience|candidate has|candidate will have)\b",
    re.IGNORECASE,
)
# Section headings — "What You'll Need to Succeed", "Who You Are" — are
# deliberately NOT read as requirement wording, neither folded into the pattern
# above nor as a rule of their own. Both were tried; both are reverted.
#
# Folding them in widens the standalone completed-degree scan, which moved 14
# Postings pass -> kill, and four of the fourteen were plainly wrong: a co-op
# asking to be "Enrolled in a Bachelor's or Master's program", a "PhD (or
# equivalent research experience)", an "advanced degree … or equivalent
# research/industry experience", and an "advanced degree … or comparable
# experience".
#
# Reading them separately, as a walk back to the nearest heading, is worse. A
# heading is not near the line it governs, so that walk was unbounded: measured
# on this corpus it governed 147 of the 298 non-snippet Postings, a median of
# 4,000 characters after the last heading and 8,986 at the widest, cancelled
# only by the five words in ``_PREFERENCE``. It defeated the very guard it was
# added beside — "Who You Are" is a culture heading, and with a benign one 1,600
# characters above it, "We love a good BS in Biology. 1 year of laboratory
# experience." was killed as a stated requirement. Replaying the whole corpus
# without the rule moved exactly one verdict, so a signal covering half the
# corpus was holding a single row; that row is held by the pattern below, from
# wording it carries itself. Widening the requirement vocabulary is its own
# change, with its own evidence.

#: Experience that presupposes the degree stated beside it. Nobody holds "0-2
#: years of postdoctoral experience" without the doctorate it follows, so a
#: degree bundled into that requirement is one the Posting is stating, not one it
#: is wishing for — and it says so inside the clause, not under a heading
#: paragraphs away.
_POST_DEGREE_EXPERIENCE = re.compile(r"\bpost[-\s]?doc(?:toral)?\b", re.IGNORECASE)
#: Wording that turns a mention of a requirement into a denial of it. A rule that
#: fires on a word *near* a requirement reads "this is not a postdoctoral
#: appointment" as though it were one; every kill signal read from a window owes
#: the Posting this much.
_NEGATED = re.compile(r"\b(?:no|not|non|without|isn't|isn’t|aren't|aren’t)\b", re.IGNORECASE)
#: How far back a negation is read from. Short on purpose: the negation that
#: matters governs the same clause ("no postdoc experience is needed"), and a
#: longer reach starts cancelling requirements a paragraph away.
_NEGATION_LOOKBACK = 30
#: How far either side of a match the wording governing it is read from. One
#: window, used by every requirement-versus-wish guard, so the completed-degree
#: scan and the bundled-degree scan cannot drift apart on the window as well.
_REQUIREMENT_CONTEXT = 100


def _stated_as_denial(text: str, match: re.Match[str]) -> bool:
    """Whether the wording carrying this match denies what it matched.

    Read from two places, because a denial sits on either side of the token it
    cancels: just before it ("no security clearance is required", "you do not
    need a PhD", "applicants without a bachelor's degree") or inside the span the
    pattern itself matched ("a security clearance is not required"). Nothing
    further out is read — a denial a sentence away is somebody else's sentence,
    and cancelling a requirement from that distance is how a heading walk went
    wrong.

    One reading, used by every signal that fires on a token appearing near a
    requirement, because the blindness is a property of that shape and not of any
    one pattern.
    """
    head = text[max(0, match.start() - _NEGATION_LOOKBACK) : match.start()]
    return bool(_NEGATED.search(head) or _NEGATED.search(match.group(0)))


def _stated_as_preference(text: str, match: re.Match[str], *, lookback: int = 70) -> bool:
    """Whether the wording governing this match calls it optional rather than required.

    Two readings, and the wide one is what catches a real Posting. Nearby: a
    preference word within ``lookback`` characters, which is the bullet's own
    wording. Governing: the nearest preference word anywhere before the match with
    no requirement word between it and the match — that is the block heading, and
    on a real Posting "Bonus Points For" sits several bullets and a few hundred
    characters above the line it governs.

    Shared deliberately. ``education_fit`` has carried this guard since it existed
    and ``work_authorization`` had none, which is why three of its five kills quote
    a bonus-points bullet. One guard used twice is the fix, not a second guard.
    """
    if "\n" in text:
        head = text[:match.start()]
        local = head.rsplit("\n", 1)[-1]
        preferences = list(_PREFERENCE.finditer(local[-lookback:]))
        if preferences:
            tail = local[-lookback:][preferences[-1].end():] + match.group(0)
            if not _REQUIREMENT.search(tail):
                return True
        for line in reversed(head.rsplit("\n", 1)[0].splitlines()):
            label = line.strip().rstrip(":")
            if re.fullmatch(r"(?:(?:required|minimum|basic|essential|preferred)\s+)?(?:qualifications|requirements|skills|experience)|bonus points for|nice to have", label, re.I):
                return bool(_PREFERENCE.search(label))
        return False
    if _PREFERENCE.search(text[max(0, match.start() - lookback) : match.start()]):
        return True
    head = text[: match.start()]
    heading = None
    for hit in _PREFERENCE.finditer(head):
        heading = hit.end()
    return heading is not None and not _REQUIREMENT.search(head[heading:])


def work_authorization_reading(posting: dict, policy: FilterPolicy = EMPTY_POLICY) -> Reading:
    """Kill the eligibility restrictions the Profile names as disqualifying.

    Two of the three ways to pass here used to read, in the store, as the third.
    ``clear`` means the Profile's restriction wording is genuinely absent.
    ``denied`` and ``preferred`` mean it was *present* and the filter waived it —
    the Posting denying the restriction ("no clearance is required") or listing
    it as a bonus. Both of those are the filter reading a negation, which is the
    reading most likely to be wrong, and both were recorded with the same reason
    string as ``clear``: "no explicit citizenship, residency, clearance…". A
    waiver that reads as an absence is the exact shape of an invisible wrong
    pass, so it gets its own token.
    """
    rules = policy.work_authorization
    description = strip_html(str(posting.get("description_html") or ""))
    denied = False
    preferred = False
    for pattern in rules.restrictions:
        for match in pattern.finditer(description):
            # A clearance listed under "Bonus Points For" excludes nobody; it is
            # the opposite of a requirement. Keep scanning rather than killing, so
            # a Posting that states the same thing as a requirement elsewhere is
            # still killed — and on the clause that actually says so.
            if _stated_as_preference(description, match):
                preferred = True
                continue
            # "No security clearance is required", "a green card is not
            # required", "this position is not ITAR-restricted": a restriction
            # the Posting is denying is the opposite of one it states, and these
            # patterns read a token with up to 55 characters of window around it,
            # so every one of them matched the denial. Keep scanning — a Posting
            # that denies one restriction may state another.
            if _stated_as_denial(description, match):
                denied = True
                continue
            return Reading(
                "kill",
                "work_authorization",
                f"work-authorization restriction: {_evidence(description, match)!r}",
            )
    for pattern in rules.noted:
        if match := pattern.search(description):
            # The Profile decided this wording is survivable, so the pass is
            # correct. Recording it as "no restriction" is not: that reads
            # downstream as a clean bill of health for a Posting saying the
            # opposite. A deliberate pass is recorded as one.
            return Reading(
                "pass",
                "work_authorization",
                f"passed deliberately — {rules.noted_label}: {_evidence(description, match)!r}",
                "noted",
            )
    # A restriction the Posting denies outranks one it merely prefers: the denial
    # is the stronger statement, and it is the one a reader auditing why this
    # Posting was let through would want to see first. The waiver rides on
    # whichever branch below the filter was already going to take — it never
    # takes a branch of its own, because a branch of its own would be a fifth
    # reason string, and the reason strings are not what this change touches.
    waived = "denied" if denied else "preferred" if preferred else None
    if _truncated(posting):
        return Reading(
            "pass", "work_authorization", _unassessed("work_authorization"), waived or "unassessed"
        )
    return Reading("pass", "work_authorization", rules.pass_reason, waived or "clear")


_DEGREE = (
    r"(?:bachelor(?:'s|’s|\s+degree)|baccalaureate(?:\s+degree)?|"
    r"master(?:'s|’s|\s+degree)|(?:b|m)\.?\s*s\.?c?\.?|b\.?\s*a\.?|"
    r"m\.\s*a\.|m\.?\s*b\.?\s*a\.?|ph\.?\s*d\.?|doctorate(?:\s+degree)?|"
    r"degree(?=\s+in\b))"
)
# A bare abbreviation is a degree only when what follows is not a product name.
# "MS Office" is Microsoft; matching it quoted an unrelated bullet as the evidence
# for a completed-degree kill, which is an unearned confidence signal on the
# dashboard even where the verdict happened to be right.
_NOT_A_DEGREE_AFTER = (
    r"(?!\s+(?:office|project|excel|word|powerpoint|power\s?point|outlook|teams|"
    r"visio|sharepoint|dynamics|azure|sql|access)\b)"
)
_DEGREE_PATTERN = re.compile(rf"\b{_DEGREE}\b{_NOT_A_DEGREE_AFTER}", re.IGNORECASE)
_DEGREE_NON_REQUIREMENT = re.compile(
    r"\b(?:current(?:ly)? (?:student|enrolled)|students? (?:pursuing|working)|"
    r"pursuing|working toward|expected to graduate|graduation expected|"
    r"not required|not necessary|no college degree necessary|or equivalent experience)\b",
    re.IGNORECASE,
)

#: An equivalence the Posting offers beside a degree: "PhD (or equivalent
#: industry experience)", "Ph.D. or equivalent", "BS/BA or equivalent". The
#: article is optional because a Posting writes it either way.
_DEGREE_EQUIVALENT = re.compile(r"\bor\s+(?:an?\s+|the\s+)?equivalent\b", re.IGNORECASE)
#: What the equivalence has to name for the requirement to still *be* a degree
#: requirement. "Equivalent" attaches to two quite different things, and only one
#: of them waives anything. "Or equivalent industry experience", "or equivalent"
#: standing alone, "or equivalent in operations" — the employer will take
#: somebody who never earned the degree, so the offer has a second branch and the
#: degree is not the bar. "Or equivalent degree", "or equivalent medical degree",
#: "or equivalent area" — the employer is naming another *degree*, or the same
#: degree in another *field of study*, and every branch of that offer is still a
#: degree. Reading the second kind as a waiver would pass Postings on a degree
#: the employer genuinely requires, which is the same silent failure as the kill
#: it is meant to fix, only in the other direction.
#:
#: Read as the head noun of the phrase the equivalence introduces: up to two
#: modifiers may sit in front of it ("equivalent medical degree"), but a function
#: word may not, because past a preposition the sentence has stopped describing
#: the equivalence and started describing something else — "equivalent experience
#: in the field" names experience, not a field of study.
_EQUIVALENT_MODIFIER = (
    r"(?:\s+(?!in\b|of\b|with\b|and\b|or\b|to\b|for\b|from\b|by\b|at\b|on\b|"
    r"the\b|an?\b)[a-z][a-z-]*)"
)
_EQUIVALENT_IS_ACADEMIC = re.compile(
    rf"^{_EQUIVALENT_MODIFIER}{{0,2}}\s+"
    r"(?:degrees?|diplomas?|qualifications?|areas?|fields?|disciplines?|majors?|"
    r"subjects?|studies|concentrations?)\b",
    re.IGNORECASE,
)
#: How far past a stated degree an equivalence may sit and still be read as a
#: branch of it. Wide enough for the list of fields a Posting puts between them —
#: "Bachelor's degree in Business, Finance, Accounting, Economics or equivalent
#: area" is 62 characters from the degree to the equivalence.
_EQUIVALENT_REACH = 90
#: Where the clause carrying the degree ends. An equivalence past it is the next
#: sentence's, and reading it as this degree's is how a guard cancels a
#: requirement a Posting really states. A full stop counts only when a space or
#: the end of the text follows it, so "Ph.D." does not end its own clause.
_CLAUSE_END = re.compile(r"[;:]|\.(?=\s|$)")


def _waived_by_an_equivalent(description: str, match: re.Match[str]) -> bool:
    """Whether the Posting offers something other than this degree in its place.

    A Posting that says "PhD (or equivalent industry experience)" has stated two
    branches of one offer, and the degree is only one of them. Read narrowly it
    is a doctorate requirement, and narrow is always the kill direction: the
    Owner is never shown a Posting whose own wording says experience answers the
    same question.

    The distinction that makes this safe is what the equivalence names, not that
    the word appears — see ``_EQUIVALENT_IS_ACADEMIC``. Three Postings in the
    committed corpus write "or equivalent" meaning another degree or another
    field of study, and every one of them still states a degree requirement.
    """
    tail = description[match.end() : min(len(description), match.end() + _EQUIVALENT_REACH)]
    # "Ph.D." carries its own final full stop, and the pattern stops at the D
    # because there is no word boundary past the dot. Left in, that dot reads as
    # the end of the clause and cancels the equivalence the Posting wrote right
    # after it — which is how "Ph.D. or equivalent with 5+ years" stayed a
    # doctorate requirement. A dotted abbreviation is the only thing this skips:
    # a degree written without dots ends its sentence at the same character.
    if "." in match.group(0) and tail.startswith("."):
        tail = tail[1:]
    if clause := _CLAUSE_END.search(tail):
        tail = tail[: clause.start()]
    equivalent = _DEGREE_EQUIVALENT.search(tail)
    return equivalent is not None and not _EQUIVALENT_IS_ACADEMIC.match(tail[equivalent.end() :])

#: How each rung of ``DEGREE_LEVELS`` is said in a Filter Decision.
_DEGREE_LABELS = {
    "high_school": "high-school qualification",
    "associate": "associate degree",
    "bachelor": "bachelor's degree",
    "master": "master's degree",
    "doctorate": "doctorate",
}
#: Wording that joins two degrees into one offer rather than stating two
#: requirements. "Bachelor's or Master's degree", "BS/MS in Biology", "BS, MS, or
#: PhD" — the employer will take whichever of them the applicant has, so the
#: easiest branch is what the offer costs. Only connective tissue may sit between
#: the two: a full stop ends the offer, and so does any word that is not part of
#: the joining, because past that the Posting has started saying something else.
_DEGREE_ALTERNATION = re.compile(r"^(?:[\s,;/]|\bor\b|\ban?\b|\bdegrees?\b)*$", re.IGNORECASE)
#: What has to be *present* between two degrees for them to be alternatives at
#: all. Without this, "Master's degree in Marketing" reads as two degrees joined
#: by a space — the level-bearing word and the noun after it — and the noun,
#: naming no level of its own, would make every such requirement unreadable.
_DEGREE_SEPARATOR = re.compile(r"[/,;]|\bor\b", re.IGNORECASE)
#: How much text two degrees may have between them and still read as one offer.
_DEGREE_ALTERNATION_GAP = 30

#: How a stated requirement stands against what the Profile says the Owner has.
#: Three values rather than two, and the third is the point: a requirement whose
#: level the Posting never said is unresolved, and an unresolved requirement
#: reaches Jev instead of killing.
_Reach: TypeAlias = Literal["met", "unsure", "exceeds"]


def _degree_level(match: re.Match[str]) -> str | None:
    """Which rung of ``DEGREE_LEVELS`` a matched degree names, if it names one.

    ``None`` is a degree whose level the Posting did not state — "a degree in a
    related field" — and returning it rather than guessing is what keeps an
    unreadable requirement out of the kill direction.
    """
    written = re.sub(r"[.\s'’]", "", match.group(0).lower())
    if written.startswith(("bachelor", "baccalaureate", "bs", "ba")):
        return "bachelor"
    if written.startswith(("master", "mba", "ms", "ma")):
        return "master"
    if written.startswith(("phd", "doctorate")):
        return "doctorate"
    return None


def _degree_rank(match: re.Match[str]) -> int:
    """Where a matched degree sits on the ladder. An unread level ranks last."""
    level = _degree_level(match)
    return DEGREE_LEVELS.index(level) if level is not None else len(DEGREE_LEVELS)


def _degree_phrases(description: str, matches: list[re.Match[str]]) -> list[re.Match[str]]:
    """One match per degree the Posting names, dropping the ones that repeat it.

    ``_DEGREE_PATTERN`` matches the level-bearing word and, in "Master's degree
    in Marketing", the noun after it as well — "degree in" is a degree in its own
    right when nothing else names one. Two matches, one degree: the second names
    no level, and left in the list it would turn every ordinary requirement into
    one that cannot be read. The first is kept because it is the half that says
    which qualification this is.
    """
    kept: list[re.Match[str]] = []
    for match in matches:
        previous = kept[-1] if kept else None
        if (
            previous is not None
            and _degree_level(match) is None
            and not description[previous.end() : match.start()].strip()
        ):
            continue
        kept.append(match)
    return kept


def _degree_offers(description: str, matches: list[re.Match[str]]) -> list[list[re.Match[str]]]:
    """Group degrees the Posting offers as alternatives to one another.

    Read in document order, the same way ``_requirements`` groups stated minima:
    consecutive degrees joined by nothing but connective tissue *and* an actual
    separator are branches of one offer, and anything else starts a new one.
    """
    offers: list[list[re.Match[str]]] = []
    for match in _degree_phrases(description, matches):
        previous = offers[-1][-1] if offers else None
        gap = description[previous.end() : match.start()] if previous is not None else ""
        if (
            previous is not None
            and len(gap) <= _DEGREE_ALTERNATION_GAP
            and _DEGREE_ALTERNATION.fullmatch(gap) is not None
            and _DEGREE_SEPARATOR.search(gap) is not None
        ):
            offers[-1].append(match)
        else:
            offers.append([match])
    return offers


def _degree_reach(level: str | None, rules: EducationFitPolicy) -> _Reach:
    """How one branch of an offer stands against what the Owner has."""
    if not rules.attainment_configured:
        # The original yes/no. It says the Owner has completed no degree, so
        # every completed-degree requirement is out of reach whatever it names,
        # and the level is not consulted at all. This is the branch that keeps a
        # Profile carrying only ``kill_completed_degree`` deciding exactly as it
        # did before the widened model existed.
        return "exceeds"
    if level is None:
        return "unsure"
    rung = DEGREE_LEVELS.index(level)
    if rules.holds is not None and rung <= DEGREE_LEVELS.index(rules.holds):
        return "met"
    if rules.in_progress is not None and rung <= DEGREE_LEVELS.index(rules.in_progress):
        # Working toward it and not holding it yet. Employers differ on whether
        # an expected graduation answers this, so the Profile cannot answer it
        # either: it goes to Jev.
        return "unsure"
    return "exceeds"


def _offer_bar(
    offer: list[re.Match[str]] | tuple[re.Match[str], ...],
    quoted: re.Match[str],
    rules: EducationFitPolicy,
) -> tuple[_Reach, re.Match[str]]:
    """What one offer costs, and the branch of it that says so.

    ``quoted`` is the branch the caller would have quoted with no grouping at
    all, and it is what comes back whenever grouping has nothing to add — which
    is every offer under ``kill_completed_degree``, so that setting keeps quoting
    the evidence it always quoted.
    """
    if not rules.attainment_configured:
        return "exceeds", quoted
    branches = [(_degree_reach(_degree_level(match), rules), match) for match in offer]
    # One branch within reach settles the offer, and one branch that cannot be
    # read settles it short of a kill. Only when every branch is out of reach is
    # the offer out of reach.
    for wanted in ("met", "unsure"):
        for reach, match in branches:
            if reach == wanted:
                return reach, match
    # Every branch exceeds, so the easiest of them is the bar this offer sets,
    # and it is the honest thing to quote. Ties keep document order.
    easiest = min(branches, key=lambda branch: (_degree_rank(branch[1]), branch[1].start()))
    return "exceeds", easiest[1]


_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
}
_YEAR_NUMBER = r"(?:\d{1,2}|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)"
# A range and a "+" are independent: "5-10+ years" is a range that is also open
# ended. Written as one alternation the range and the plus were mutually
# exclusive, so "5-10+" could not match at the 5, re-anchored at the 10, and
# reported the ceiling as the floor. Keeping them separate reads the floor.
_YEARS_SPAN = (
    rf"(?:\s*(?:-|–|—|to)\s*(?P<maximum>{_YEAR_NUMBER}))?(?:\s*(?:\+|plus)|\s+or more)?"
)
_YEARS_EXPERIENCE = re.compile(
    rf"\b(?:(?:at least|minimum(?: of)?|more than|over)\s+)?"
    rf"(?P<minimum>{_YEAR_NUMBER})(?:\s*\(\d{{1,2}}\))?"
    rf"{_YEARS_SPAN}"
    rf"\s+years?(?:['’])?(?:\s+(?:of|in))?\s+[^.;:]{{0,90}}?\bexperience\b",
    re.IGNORECASE,
)
_YEARS_IN_FIELD = re.compile(
    rf"\b(?:(?:at least|minimum(?: of)?|over)\s+)?(?P<minimum>{_YEAR_NUMBER})(?:\s*\(\d{{1,2}}\))?"
    rf"{_YEARS_SPAN}"
    rf"\s+years?\s+(?:in|building|working|developing|leading|managing|conducting|"
    rf"designing|delivering|supporting|programming|engineering)\b[^.;:]{{0,100}}",
    re.IGNORECASE,
)
_QUALIFICATION_CONTEXT = re.compile(
    r"\b(?:requires?|requirements?|qualifications?|must have|minimum|about you|"
    r"your experience includes|ideal candidate|you have)\b",
    re.IGNORECASE,
)
# Company-bio boilerplate ("with over 25 years in the staffing business") is about
# the employer, not the candidate — it must never read as an experience minimum.
_COMPANY_BOILERPLATE = re.compile(
    r"\b(?:in business|in the [a-z]+ (?:business|industry)|we(?:'ve| have) been|"
    r"has been (?:providing|serving|delivering|placing|connecting)|industry leader)\b",
    re.IGNORECASE,
)


def _number(value: str) -> int:
    return int(value) if value.isdigit() else _NUMBER_WORDS[value.lower()]


#: Wording that ties a stated minimum to the one before it as part of a single
#: requirement. "4 years of collective IT experience, with 2 years of direct
#: support experience" is one bar of four qualified by a sub-clause, not a ladder
#: whose lower rung is two — and reading it as a ladder passed a Posting nobody
#: with two years could hold.
_CONJUNCTIVE_JOIN = re.compile(
    r"^[\s,;:)\-–—]*(?:with|including|of which|and|plus)\b", re.IGNORECASE
)
#: What separates one branch of a requirement from another.
_ALTERNATIVE = re.compile(r"\bor\b", re.IGNORECASE)
#: How much text two minima may have between them and still read as one
#: requirement. Beyond this they are separate bullets, which is a ladder.
_CONJUNCTIVE_GAP = 40
#: How far ahead of a stated minimum a degree may sit and still be bundled with
#: it. "PhD in a related discipline with 0-2 years" is one requirement: its years
#: are within reach and its degree is not, and satisfying half of it satisfies
#: nothing.
_DEGREE_BUNDLE_LOOKBACK = 60


@dataclass(frozen=True)
class _Requirement:
    """One stated requirement. Every condition in it has to hold at once."""

    years: int
    evidence: re.Match[str]
    degree: re.Match[str] | None
    #: The degrees this requirement's degree is offered alongside, as branches of
    #: one offer. Empty when the requirement states no degree at all.
    degree_offer: tuple[re.Match[str], ...] = ()

    def reach(self, rules: EducationFitPolicy) -> tuple[_Reach, re.Match[str] | None]:
        """How far out of reach this requirement is, and the degree branch that says so.

        Every condition in a requirement counts at once, so the years decide
        first: a stated minimum the Owner cannot meet puts the whole requirement
        out of reach whatever degree sits beside it.
        """
        if rules.experience_years_kill is not None and self.years >= rules.experience_years_kill:
            return "exceeds", None
        if self.degree is None:
            return "met", None
        return _offer_bar(self.degree_offer or (self.degree,), self.degree, rules)


def _context(description: str, start: int, end: int) -> str:
    """The wording governing a span: the span itself and a window either side."""
    return description[
        max(0, start - _REQUIREMENT_CONTEXT) : min(len(description), end + _REQUIREMENT_CONTEXT)
    ]


#: The preference vocabulary the *years* scan has always read, so the degree
#: scan can read the same. There was never a reason for the two to differ: a
#: Posting writing "a plus" about its degree got no guard at all while one
#: writing "a plus" about its years got one.
_PREFERENCE_WORDS = (
    r"preferred|ideal(?:ly)?|a plus|a bonus|bonus|nice to have|desirable|advantageous|welcome"
)
_PREFERRED_AFTER = re.compile(rf"\b(?:{_PREFERENCE_WORDS})\b", re.IGNORECASE)
#: A parenthetical holding nothing but the preference statement itself — "(strongly
#: preferred)", "(a plus)". Anything else in the bracket and the preference is
#: about what the bracket holds, not about the degree the sentence opened with.
_BARE_PREFERENCE = re.compile(
    rf"\s*(?:is\s+)?(?:highly|strongly|very|much|most)?\s*(?:{_PREFERENCE_WORDS})\b",
    re.IGNORECASE,
)


def _preference_clause(tail: str) -> int:
    """Where the clause carrying a degree ends, for a forward preference reading.

    The sentence boundary, and then something a backward reading never had to
    think about: a bracket. A parenthetical is a sub-clause and what it holds
    belongs to the word it follows. Beam Therapeutics writes "BS degree in a
    technical discipline (Engineering preferred)" — the *discipline* is
    preferred, the degree is required, and reading the bracket as a statement
    about the degree waives a degree the Posting asks for. That is the same
    misreading as an "equivalent" that names a field of study, one guard along,
    so a preference bracket must not waive a degree requirement.

    A bracket holding nothing but the preference itself — "a Bachelor's degree
    (strongly preferred)" — is still about the degree, so it does not end the
    clause.
    """
    end = len(tail)
    if clause := _CLAUSE_END.search(tail):
        end = clause.start()
    for bracket in re.finditer(r"\(", tail[:end]):
        if not _BARE_PREFERENCE.match(tail[bracket.end() : end]):
            return bracket.start()
    return end


#: How far past a degree a preference word may sit and still be read as
#: describing it. Short on purpose. The reading is cancelled by a requirement
#: word in between (below), but a bound is still owed: an unbounded forward
#: reach is the heading walk this module already reverted once, pointed the
#: other way, and it would let one "a plus" at the foot of a Posting waive every
#: degree above it.
_PREFERENCE_REACH = 60


def _preferred_after(description: str, match: re.Match[str]) -> bool:
    """Whether the wording *following* this degree calls it a preference.

    ``_stated_as_preference`` reads backwards — the bullet's own wording and the
    block heading above it — and a Posting that puts the word after the degree
    was left to a test anchored at the character straight after the match. That
    anchor meant anything at all in between defeated it: one adverb in "PhD
    highly preferred", and the noun in "A Bachelor's degree is preferred", where
    the match lands on "Bachelor's" and "degree" sits between it and the word
    that governs it. The failure was adjacency, never adverbs, so widening the
    anchor by a word would have left the second standing.

    Bounded twice, because a forward reading that cancels kills is exactly as
    dangerous as a backward one. It stops at the end of the clause and at
    ``_PREFERENCE_REACH`` characters, and a requirement word between the degree
    and the preference word cancels it outright — "Bachelor's degree required,
    Salesforce experience a plus" states a requirement and then something else,
    and the "a plus" is about the something else.
    """
    tail = description[match.end() : min(len(description), match.end() + _PREFERENCE_REACH)]
    if "." in match.group(0) and tail.startswith("."):
        tail = tail[1:]
    tail = tail[: _preference_clause(tail)]
    preference = _PREFERRED_AFTER.search(tail)
    return preference is not None and not _REQUIREMENT.search(tail[: preference.start()])


def _degree_is_optional(description: str, match: re.Match[str]) -> bool:
    """The guards the completed-degree scan applies, so a bundle reads them too."""
    context = _context(description, match.start(), match.end())
    return bool(
        _DEGREE_NON_REQUIREMENT.search(context)
        # The same offer in the phrasings that pattern does not list, and read
        # from the clause rather than from the 100-wide context, because an
        # equivalence belongs to the degree it sits beside.
        or _waived_by_an_equivalent(description, match)
        # The same wording ``_DEGREE_NON_REQUIREMENT`` reads, in the phrasings it
        # does not list: "you do not need a PhD", "applicants without a
        # bachelor's degree". Read from 30 characters rather than the 100-wide
        # context, so "able to work without supervision" two sentences away
        # cannot cancel a degree the Posting really requires.
        or _stated_as_denial(description, match)
        or _stated_as_preference(description, match, lookback=65)
        # The other half of the same reading. Kept in ``_degree_is_optional``
        # rather than folded into ``_stated_as_preference``, deliberately: that
        # guard is shared with ``work_authorization``, where loosening it would
        # move a filter this change has no evidence about. Only the degree scan
        # and the bundled-degree scan read this one.
        or _preferred_after(description, match)
    )


def _presupposes_the_degree(
    description: str, degree: re.Match[str], minimum: re.Match[str]
) -> bool:
    """Whether the experience this requirement asks for presupposes the degree in it.

    Nobody holds "0-2 years of postdoctoral experience" without the doctorate the
    same clause names, so a Posting wording it that way has stated the degree as
    a requirement in its own words — no heading needed.

    The reading is bounded twice, and both bounds are the same defect caught
    twice. It reads only the span from the degree to the end of the stated
    minimum — the requirement itself — because the ±100-character window this
    once used reached outside the clause and killed a Posting for "our postdoc
    alumni network runs monthly seminars", for "you will report to a
    postdoctoral fellow", and for "1 year of laboratory experience, equivalent
    to postdoctoral training", every one of them differing from a passing
    control by that one word. And it skips a mention the Posting is denying:
    "no postdoc experience is needed" and "this is not a postdoctoral
    appointment" say the opposite of the thing being read from them.

    The span deliberately includes the stated minimum rather than stopping at
    its start: the corpus Posting this whole reading exists for writes it inside
    the years — "PhD in a related discipline with 0-2 years of postdoctoral or
    industry experience" — and stopping short would drop the one row it holds.
    """
    return any(
        not _stated_as_denial(description, found)
        for found in _POST_DEGREE_EXPERIENCE.finditer(description, degree.end(), minimum.end())
    )


def _bundled_degree(description: str, match: re.Match[str]) -> re.Match[str] | None:
    """A completed degree stated as part of the same requirement as this minimum."""
    start = max(0, match.start() - _DEGREE_BUNDLE_LOOKBACK)
    bundled = None
    for degree in _DEGREE_PATTERN.finditer(description, start, match.start()):
        # An "or" between the two puts them in different branches of the same
        # sentence — "MS … or a related field with 4+ years" asks for the years,
        # not for the MS — so only an unbroken clause bundles them.
        if _ALTERNATIVE.search(description[degree.end() : match.start()]):
            continue
        if _degree_is_optional(description, degree):
            continue
        # The requirement-versus-wish guard the completed-degree scan applies,
        # which this scan simply did not have: "We love a good BS in Biology.
        # 1 year of laboratory experience." bundled a degree nobody had asked
        # for. The gap was invisible in the corpus because the 13 Postings it
        # would have moved all state at least three years, which is exactly the
        # threshold the years rule kills on — accidental correctness, and a
        # golden fixture cannot tell it from the real thing.
        #
        # Read over the whole requirement rather than the degree alone: a bundle
        # is the degree *and* the years, and a window around either half alone
        # cuts the other in two. Nothing here reads a heading, and
        # ``_POST_DEGREE_EXPERIENCE`` says why not — but the two signals are not
        # bounded alike, and a comment that once said they were was wrong. The
        # requirement wording is read from a window either side of the clause,
        # because "Requirements:" introduces it from outside. The experience
        # signal is read from *inside* the clause only, by
        # ``_presupposes_the_degree``, because a word near a requirement is not
        # a claim the requirement makes.
        context = _context(description, degree.start(), match.end())
        if not (
            _REQUIREMENT.search(context) or _presupposes_the_degree(description, degree, match)
        ):
            continue
        bundled = degree
    return bundled


def _offer_around(description: str, degree: re.Match[str]) -> tuple[re.Match[str], ...]:
    """The branches the bundled degree is offered alongside.

    ``_bundled_degree`` deliberately steps over a branch with an "or" between it
    and the minimum, because that branch is not what the years are bundled with.
    The easier branch is still on offer, though — "Bachelor's or Master's degree
    with 2 years" will take either — so the offer is reassembled here rather than
    left as the one branch that happened to sit closest to the number.
    """
    for offer in _degree_offers(description, list(_DEGREE_PATTERN.finditer(description))):
        if any(branch.span() == degree.span() for branch in offer):
            return tuple(offer)
    return (degree,)


def _requirements(
    description: str, stated: list[tuple[int, re.Match[str]]], *, bundle_degree: bool
) -> list[_Requirement]:
    """Group stated minima into the requirements a Posting actually states.

    Two readings of the same list of numbers, and the difference is the whole
    defect. Minima joined by a conjunction inside one clause are conditions of one
    requirement, so the *largest* of them is that requirement's bar. Minima that
    are simply listed apart are separate rungs on offer, and the Posting is open
    to whoever clears the *easiest* of them.
    """
    grouped: list[list[tuple[int, re.Match[str]]]] = []
    for minimum, match in stated:
        previous = grouped[-1][-1][1] if grouped else None
        gap = description[previous.end() : match.start()] if previous is not None else ""
        conjunctive = (
            previous is not None
            and len(gap) <= _CONJUNCTIVE_GAP
            and _CONJUNCTIVE_JOIN.match(gap) is not None
            and not _ALTERNATIVE.search(gap)
        )
        if conjunctive:
            grouped[-1].append((minimum, match))
        else:
            grouped.append([(minimum, match)])
    requirements = []
    for group in grouped:
        years, evidence = max(group, key=lambda condition: condition[0])
        degree = _bundled_degree(description, group[0][1]) if bundle_degree else None
        requirements.append(
            _Requirement(
                years=years,
                evidence=evidence,
                degree=degree,
                degree_offer=_offer_around(description, degree) if degree is not None else (),
            )
        )
    return requirements


def _required_degree(match: re.Match[str], rules: EducationFitPolicy) -> str:
    """How a kill names the degree it is killing on.

    Under ``kill_completed_degree`` this is the wording it has always been: the
    Owner has completed no degree, the level was never consulted, and claiming to
    have read one would be an unearned confidence signal. Under the widened model
    the level *is* what decided, so the reason says which level and what it is
    above.
    """
    if not rules.attainment_configured:
        return "a completed degree"
    level = _degree_level(match)
    named = _DEGREE_LABELS[level] if level is not None else "degree"
    return f"a completed {named} above the {_attainment(rules)}"


def _attainment(rules: EducationFitPolicy) -> str:
    """What the Profile says the Owner has, said the way a reader would say it.

    Only ever reached with the widened model configured, so one of the two is set.
    """
    if rules.holds is not None:
        return f"{_DEGREE_LABELS[rules.holds]} the Profile states the Owner holds"
    return f"{_DEGREE_LABELS[rules.in_progress or 'high_school']} the Profile states is in progress"


def _education_fit_pass_reason(rules: EducationFitPolicy) -> str:
    """State what the Profile looked for, so a pass says what it survived."""
    looked_for = []
    if rules.attainment_configured:
        looked_for.append(f"completed-degree requirement above the {_attainment(rules)}")
    elif rules.kill_completed_degree:
        looked_for.append("clear completed-degree requirement")
    if rules.experience_years_kill is not None:
        looked_for.append(f"{rules.experience_years_kill}+ year experience minimum")
    if not looked_for:
        return "no education-fit requirement is configured"
    return "no " + " or ".join(looked_for)


def _graduate_enrollment_reading(description: str, rules: EducationFitPolicy) -> Reading | None:
    """Compare complete enrollment clauses, not an internship's title."""
    for block_match in re.finditer(r"[^\n]+", description):
        block = block_match.group(0)
        enrollment = re.search(r"\b(?:currently\s+)?(?:pursuing|enrolled in)\b", block, re.I)
        if not enrollment:
            continue
        degrees = list(_DEGREE_PATTERN.finditer(block))
        levels = {_degree_level(degree) for degree in degrees}
        if not levels or None in levels or not levels.issubset({"master", "doctorate"}):
            continue
        if re.search(r"\bor equivalent\b|\bnot required\b", block, re.I) or _stated_as_preference(description, block_match) or all(_preferred_after(block, degree) for degree in degrees):
            continue
        if re.search(r"\bwill\b|\bby (?:the )?(?:start|beginning)\b", block, re.I):
            return Reading("pass", "education_fit", f"future graduate enrollment timing is not confirmed: {block!r}", "enroll_unknown")
        if rules.current_student_level == "undergraduate":
            return Reading("kill", "education_fit", f"graduate-only enrollment conflicts with the Profile's stated undergraduate status: {block!r}")
        if rules.current_student_level is None:
            return Reading("pass", "education_fit", f"graduate enrollment is required but current student level is not confirmed: {block!r}", "enroll_unknown")
    return None


def education_fit_reading(posting: dict, policy: FilterPolicy = EMPTY_POLICY) -> Reading:
    """A known education conflict outranks an independent enrollment unknown."""
    description = strip_html(str(posting.get("description_html") or ""), preserve_blocks=True)
    enrollment = _graduate_enrollment_reading(description, policy.education_fit)
    attainment = _education_attainment_reading(posting, policy)
    if enrollment is not None and enrollment.verdict == "kill":
        return enrollment
    if attainment.verdict == "kill":
        return attainment
    return enrollment or attainment


def _education_attainment_reading(posting: dict, policy: FilterPolicy = EMPTY_POLICY) -> Reading:
    """Kill completed-degree requirements and experience minima the Profile rules out.

    A Posting states one or more requirements and is open to whoever meets any
    one of them, so it dies only when every one of them is out of reach. What a
    single requirement costs is everything it names at once: "4 years of
    collective IT experience, with 2 years of direct support experience" is one
    bar of four, and "PhD in a related discipline with 0-2 years" wants the
    doctorate as much as it wants the years. Reading each stated number as its
    own rung passed both of those — the ladder rule applied to a sentence that is
    not a ladder.

    Three quite different passes shared one reason string. ``met`` is a Posting
    that stated a bar and the Owner clears it; ``optional_degree`` is one that
    asked for a degree and was read as not requiring it; ``clear`` is one that
    stated nothing. The reason on all three was "no clear completed-degree
    requirement or N+ year experience minimum", which is true of ``clear`` and
    misleading of the other two — it reports an absence where the rule made a
    judgement. The token reports the judgement.

    The widened model adds two readings of its own, and they are separate tokens
    because they are opposite findings. ``degree_met`` is a stated degree
    requirement the Profile placed at or below what the Owner holds — a bar read
    and cleared, which without its own token would record as ``clear`` and be
    indistinguishable from a Posting that asked for no qualification at all.
    ``degree_unplaced`` is a stated degree requirement the Profile could *not*
    place: a level the Posting never named, or one at a qualification still in
    progress. That is a pass that abstained, and it is the reading to audit
    first, because it is the one where a wrong rule would be passing Postings it
    does not understand rather than clearing ones it does.

    The token is therefore finer than the branch: it is computed from what the
    two scans read, not from which ``return`` was taken, so a truncated Posting
    that nonetheless stated a bar the Owner clears reads ``met`` while its reason
    still says the description could not be assessed. That is deliberate. The
    reason is the filter's conclusion about the Posting as a whole and is not
    changed here; the token is what the rule actually read, which is the thing an
    audit needs and the thing prose kept losing.
    """
    rules = policy.education_fit
    description = strip_html(str(posting.get("description_html") or ""), preserve_blocks=True)

    # Both scans exist only to compare a stated minimum against
    # experience_years_kill, so neither runs when the Profile has not set one —
    # an unguarded scan would reach the comparison below with None.
    experience_matches: list[re.Match[str]] = []
    if rules.experience_years_kill is not None:
        experience_matches.extend(_YEARS_EXPERIENCE.finditer(description))
        experience_matches.extend(
            match
            for match in _YEARS_IN_FIELD.finditer(description)
            if _QUALIFICATION_CONTEXT.search(description[max(0, match.start() - 110) : match.start()])
        )
    # Collected in document order and grouped into requirements below: the lowest
    # rung on offer is the bar, but only across rungs a Posting really offers.
    stated: list[tuple[int, re.Match[str]]] = []
    for match in sorted(experience_matches, key=lambda value: value.start()):
        before = description[max(0, match.start() - 70) : match.start()]
        after = description[match.end() : min(len(description), match.end() + 35)]
        if re.search(r"\bup to\s*$", before, re.IGNORECASE):
            continue
        if _stated_as_preference(description, match) or re.match(
            r"^\s*(?:is\s+)?(?:preferred|ideal|a plus|a bonus)\b", after, re.IGNORECASE
        ):
            continue
        if _COMPANY_BOILERPLATE.search(f"{before} {match.group(0)} {after}"):
            continue
        minimum = _number(match.group("minimum"))
        if minimum > rules.experience_years_implausible:
            continue  # implausible as a candidate requirement — company-history talk
        stated.append((minimum, match))
    # A requirement is a bundle: it is met only when every condition in it is.
    # The Posting survives when any one of the requirements it states is met — a
    # ladder still passes on its lowest rung — and dies only when none of them is.
    requirements = _requirements(description, stated, bundle_degree=rules.reads_degrees)
    assessed = [(requirement, *requirement.reach(rules)) for requirement in requirements]
    # A bundle the Profile could not place is a pass that abstained, not a pass
    # that cleared a bar, and the token has to be able to say which. Set where
    # the assessment happens so it cannot drift from it: no requirement within
    # reach, and at least one whose level the Posting never stated.
    unplaced_requirement = (
        bool(assessed)
        and not any(reach == "met" for _, reach, _ in assessed)
        and any(reach == "unsure" for _, reach, _ in assessed)
    )
    if assessed and all(reach == "exceeds" for _, reach, _ in assessed):
        easiest, _, quoted = min(assessed, key=lambda item: item[0].years)
        if quoted is not None and easiest.years < rules.experience_years_kill:
            return Reading(
                "kill",
                "education_fit",
                f"requires {_required_degree(quoted, rules)} alongside {_years(easiest.years)} of "
                f"experience, and no stated alternative asks for less: "
                f"{_evidence(description, quoted)!r}",
            )
        return Reading(
            "kill",
            "education_fit",
            f"requires at least {_years(easiest.years)} of experience: "
            f"{_evidence(description, easiest.evidence)!r}",
        )

    # The degrees the Posting states outside any experience requirement. Grouped
    # into offers first, because "Bachelor's or Master's" is one requirement open
    # at two rungs and reading the Master's alone would kill somebody the
    # employer would take. Under ``kill_completed_degree`` every branch is out of
    # reach anyway, so grouping changes neither verdict nor evidence there.
    #
    # ``unbinding_degree`` is set where the scan already decides, so the token
    # cannot drift from it: a degree the rule read and did not treat as binding
    # is exactly the thing a pass used to swallow.
    unbinding_degree = False
    placed_degree = False
    if rules.reads_degrees:
        stated_degrees: list[re.Match[str]] = []
        # A degree the Posting offered to take something else in place of. It
        # cannot be the evidence for a kill — the Posting said so — but it is not
        # simply struck from the reading either, because that is not what it is.
        # A wish states no way in, so dropping it leaves the Posting's other
        # requirements standing, which is right. An equivalence *is* a way in,
        # and a way in the Owner may already be through: "Requirements: PhD in
        # Chemistry. Qualifications: Bachelor's degree or equivalent in Biology."
        # states two, and an Owner holding a bachelor's meets the second. Struck
        # out, it left the doctorate alone to kill a Posting that had passed.
        waived_degrees: list[re.Match[str]] = []
        for match in _DEGREE_PATTERN.finditer(description):
            if _degree_is_optional(description, match):
                unbinding_degree = True
                if _waived_by_an_equivalent(description, match):
                    waived_degrees.append(match)
                continue
            if _REQUIREMENT.search(_context(description, match.start(), match.end())):
                stated_degrees.append(match)
            else:
                unbinding_degree = True
        # Grouped together so a waived branch and the branches beside it stay one
        # offer, then split apart again: an offer every branch of which was
        # waived is not a requirement this Posting states.
        waived_spans = {match.span() for match in waived_degrees}
        ordered = sorted(stated_degrees + waived_degrees, key=lambda match: match.start())
        binding: list[tuple[_Reach, re.Match[str]]] = []
        waived_within_reach = False
        for offer in _degree_offers(description, ordered):
            reach, quoted = _offer_bar(offer, offer[0], rules)
            if all(branch.span() in waived_spans for branch in offer):
                # Counts only in the passing direction, and only when the Profile
                # placed it within reach. Under ``kill_completed_degree`` nothing
                # is ever within reach, so a waived offer contributes nothing
                # there and a degree requirement stated elsewhere still kills —
                # which is what two corpus Postings need. Ginkgo Bioworks states
                # "PhD (or equivalent experience)" in one paragraph and "Minimum
                # Qualifications Education: Ph.D. … or a Master's degree with 12+
                # years" in another; the second is a requirement whatever the
                # first says.
                waived_within_reach = waived_within_reach or reach == "met"
                continue
            binding.append((reach, quoted))
        if binding and not waived_within_reach and all(reach == "exceeds" for reach, _ in binding):
            quoted = binding[0][1]
            return Reading(
                "kill",
                "education_fit",
                f"requires {_required_degree(quoted, rules)}: {_evidence(description, quoted)!r}",
            )
        if (
            binding
            and not waived_within_reach
            and not any(reach == "met" for reach, _ in binding)
        ):
            # Every offer either exceeds or could not be read, and one could not
            # be read. That is "not sure", and it reaches Jev rather than
            # deciding either way here. The token says so too: an abstention and
            # a cleared bar are different readings and must not record alike.
            unread = next(match for reach, match in binding if reach == "unsure")
            return Reading(
                "pass",
                "education_fit",
                f"states a completed-degree requirement this Profile cannot place against the "
                f"{_attainment(rules)}: {_evidence(description, unread)!r}; passed conservatively",
                "degree_unplaced",
            )
        # An offer the Profile placed within reach. Recorded, because otherwise a
        # Posting that asked for a bachelor's and got one is indistinguishable in
        # the store from one that asked for no qualification at all — the pass
        # this filter used to swallow, in the one direction the widened model
        # newly creates.
        # Deliberately not set by a waived offer within reach. That reading is
        # "the rule read a degree and decided it was not binding", which is
        # ``optional_degree`` and is the finer of the two.
        placed_degree = any(reach == "met" for reach, _ in binding)

    fact = (
        "unconfigured"
        if not rules.reads_degrees and rules.experience_years_kill is None
        else "degree_unplaced"
        if unplaced_requirement
        else "met"
        if requirements
        else "degree_met"
        if placed_degree
        else "optional_degree"
        if unbinding_degree
        else "unassessed"
        if _truncated(posting)
        else "clear"
    )
    # Nothing was found — but on a truncated snippet the requirements section is
    # not in the text at all, so "nothing was found" is not a finding about the
    # Posting. Say which it is.
    if _truncated(posting):
        return Reading("pass", "education_fit", _unassessed("education_fit"), fact)
    return Reading("pass", "education_fit", _education_fit_pass_reason(rules), fact)


def _rungs(rules: RoleTargetPolicy, band: tuple[int, int]) -> str:
    """Name the accepted rungs, for a reason that says which band a title missed.

    Takes the band it is naming rather than looking one up, so it is structurally
    unable to render nothing. A band exists only where at least one rung is
    accepted; with none accepted this used to return ``""`` and the kill reason
    read "above the  seniority band the Profile targets" — a confident kill
    naming a band that was not there. If a reason names a band, there is a band.
    """
    lowest, highest = band
    names = [
        level.name
        for index, level in enumerate(rules.levels)
        if lowest <= index <= highest and level.name in rules.accept
    ]
    if len(names) > 2:
        return f"{', '.join(names[:-1])} and {names[-1]}"
    return " and ".join(names)


def role_target_reading(posting: dict, policy: FilterPolicy = EMPTY_POLICY) -> Reading:
    """Kill titles outside the band of the ladder the Profile targets.

    A preference on a scale, not a test for one kind of person: the Profile writes
    the rungs and names the ones it accepts, so a student targeting the bottom of
    the ladder and a mid-career applicant targeting its middle are the same rule
    with different data. Conservative on purpose — the title is the only field
    that survives an aggregator's truncation, so a title naming no rung at all is
    left unplaced and passes to Jev rather than being guessed at.

    A Posting offering more than one title is read the way ``education_fit`` reads
    a ladder of stated minima: the least demanding *offer* decides. "Engineer II /
    Senior Software Engineer" is open at Engineer II, and killing it on its senior
    half reads a ladder as a floor — the same defect the experience filter already
    fixed, in the same filter pass, in the opposite direction. Words *inside* one
    offered title read the same way: "Senior Research Associate" is ambiguous
    about its rung, so it reaches Jev as an Associate.

    Only the leading segment of a title names a rung at all. What follows the
    first comma, bracket or dash is a department, a product, a site or a shift,
    and scanning it for rung words placed "Research Associate, Head and Neck
    Oncology" on the executive rung. That is a property of the rule, not of one
    Profile's word list: every Profile's ladder has domain words that double as
    seniority words, and no amount of curating a list fixes the reading.

    A ladder the Profile accepts no rung of is the same conservative case one
    level up: it can place nothing, so it decides nothing. That is not the
    Profile saying "the bottom rung only" — it is the Profile saying nothing at
    all about where on its ladder it wants to be, and the difference is a corpus
    of Postings the Owner never learns existed.

    The fact recorded on a pass is the rung itself — the Profile's own name for
    the level the title placed at — and ``unplaced`` where the ladder read a rung
    but the Profile's band did not accept it or the ladder read none at all.
    That distinction is the one this filter's own docstring had to be corrected
    over: commit 4d64c1e claimed 12 recoveries "offer a rung this Profile
    targets", and measuring the corpus showed 11 of the 12 were the ladder
    abstaining. The correction took reading the code. Recorded, it is a count.
    An empty band is a third thing again — the ladder did not abstain, the
    Profile did — so it is ``unbanded`` rather than either of those.
    """
    rules = policy.role_target
    title = str(posting.get("title") or "")
    if not rules.configured:
        return Reading(
            "pass", "role_target", "no role target is configured; passed conservatively", "unconfigured"
        )

    # One reading of the title, for both branches below. ``exclude`` used to read
    # the whole title while the ladder read the role, so the fix made for the
    # ladder — a department is not the job — was missing one line above it, and
    # ``exclude`` is the branch that runs first and has no conservative escape at
    # all: it killed "Field Marketing Manager, Account Executive Support" and
    # "Marketing Coordinator, Recruiter Enablement", marketing roles cut on the
    # name of the team they support. Both branches now read what
    # ``alternatives`` returns, so there is one place to be right.
    offers = rules.alternatives(title)
    for offer in offers:
        if excluded := rules.exclude.search(offer):
            return Reading(
                "kill",
                "role_target",
                f"title names {rules.exclude.label} ({excluded.group(0)!r}): {title!r}",
            )

    # A comma normally introduces context, but a context phrase can itself
    # name an excluded function: ``Contractor, Loading Dock Associate`` is a
    # loading-dock role even though the leading segment says only Contractor.
    # Require the excluded wording to be followed by a rung noun from this
    # Profile before treating it as a function; that preserves legitimate
    # contexts such as ``Research Associate, Sales Enablement`` while respecting
    # a complete excluded role phrase.  The check is intentionally generic and
    # uses the Profile's own ladder rather than a hard-coded loading-dock rule.
    if rules.exclude.configured:
        for excluded in rules.exclude.pattern.finditer(title) if rules.exclude.pattern else ():
            suffix = title[excluded.end() :]
            if any(level.titles.search(suffix) for level in rules.levels):
                return Reading(
                    "kill",
                    "role_target",
                    f"title names {rules.exclude.label} ({excluded.group(0)!r}): {title!r}",
                )

    # A ladder with no accepted rung cannot place anything. ``accept`` empty or
    # absent is a Profile that has said nothing about where on its ladder it
    # wants to be — not one that has said "the bottom rung only", which is what
    # the old ``(0, 0)`` band asserted on its behalf while killing every rung
    # above it. The rule this filter cannot break applies here exactly as it does
    # to a title the ladder cannot read: what cannot be resolved reaches
    # Jev. ``exclude`` above is independent of the band and has already run,
    # because a Profile can name functions it does not want without naming a
    # rung it does.
    band = rules.band
    if band is None:
        # Its own token, not ``unplaced``: the ladder did not abstain here, the
        # Profile did. ``unplaced`` is a title this ladder could not read, and
        # the answer to it is a better rung vocabulary; ``unbanded`` is a Profile
        # that named no rung to place anything in, and the answer to it is the
        # Profile. Recording both as one would hide a Profile that has silently
        # stopped filtering on seniority inside a count of hard titles.
        return Reading(
            "pass",
            "role_target",
            f"the Profile names no {rules.label} to place the title in; passed conservatively",
            "unbanded",
        )

    # One placement per offer, ``None`` where an offer names no rung: a Posting
    # open at "Engineer II / Senior Software Engineer" is open at both, and
    # reading only the half further from the band kills a Posting whose other
    # half the Owner is targeting. Reading a *rung* is the whole job here, so an
    # offer that names none is a title this filter must not decide.
    readings = tuple(rules.place(offer) for offer in offers)
    lowest, highest = band
    placements = [placed for placed in readings if placed is not None]
    accepted = [placed for placed in placements if lowest <= placed[0] <= highest]
    if accepted:
        _, level, _ = min(accepted, key=lambda reading: reading[0])
        return Reading(
            "pass",
            "role_target",
            f"title names {_article(level.name)} {level.name} role, within the {rules.label}",
            level.name,
        )
    # An offer the ladder cannot read is one the Owner might want. A gap in the
    # rung vocabulary has to resolve to "not sure" and reach Jev, never to
    # "no": the word list is a convenience, never the thing keeping a Posting safe.
    #
    # Correction to the record. The commit that added the lowest-offer rule
    # (4d64c1e) said all 12 of its role_target recoveries "offer a rung this
    # Profile targets". Measured against the corpus, 11 of the 12 leave here —
    # "Scientist II / Senior ML Scientist", "Engineer II /Senior Software
    # Engineer" and the like, where neither half places in band and the pass is
    # this escape declining to decide. Only
    # greenhouse:lilasciences:4310496009 ("Senior Research Associate /
    # Associate Scientist") actually places, at entry. The rule and the tests
    # were right; the claim about why they worked was not, and the difference
    # matters: a recovery that is really an abstention is a Posting Jev has to
    # qualify, not a Posting the ladder understood.
    if len(placements) < len(readings):
        return Reading(
            "pass",
            "role_target",
            f"title names no {rules.label} level; passed conservatively",
            "unplaced",
        )

    index, level, match = min(placements, key=lambda reading: rules.distance_from_band(reading[0]))
    edge = (
        f"{'above' if index > highest else 'below'} the {_rungs(rules, band)} "
        f"{rules.label} the Profile targets"
    )
    # ``match.string`` is the offer this rung was read from — the ladder searched
    # that fragment — so this asks whether that one offer named more than one
    # rung. When it did, the rung being quoted is the reading most favourable to
    # the Owner rather than the only one available, and saying "title names a
    # senior role ('Staff')" of "Chief of Staff" reads like a misreading of the
    # title instead of the kill it is. Ambiguity is the difference the Owner is
    # owed, and it is the title's property, not any one title's special case.
    if len(rules.namings(match.string)) > 1:
        return Reading(
            "kill",
            "role_target",
            f"read as close to the {rules.label} as this ladder allows, the title is "
            f"{_article(level.name)} {level.name} role ({match.group(0)!r}), still {edge}: {title!r}",
        )
    return Reading(
        "kill",
        "role_target",
        f"title names {_article(level.name)} {level.name} role ({match.group(0)!r}), {edge}: {title!r}",
    )


#: Weekend and night wording. Conservative on purpose: a false kill is worse
#: than one extra Posting for Jev. Title tokens are the Thermo weekend
#: technician case. Description tokens require ``shift`` so a benefits line
#: about a gym that is open on weekends does not fire.
_SHIFT_WEEKEND = re.compile(
    r"\bweekend(?:s)?\s+(?:shift|schedule|hours|position|role|technician|tech)\b"
    r"|\b(?:saturday|sunday)\s+(?:shift|schedule|hours)\b",
    re.IGNORECASE,
)
_SHIFT_WEEKEND_TITLE = re.compile(r"\bweekend(?:s)?\b", re.IGNORECASE)
_SHIFT_NIGHT = re.compile(
    r"\b(?:nights?|overnight|graveyard|third)\s+shifts?\b",
    re.IGNORECASE,
)


def _first_shift_hit(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    for match in pattern.finditer(text):
        if _stated_as_preference(text, match) or _stated_as_denial(text, match):
            continue
        return match
    return None


def shift_preference_reading(posting: dict, policy: FilterPolicy = EMPTY_POLICY) -> Reading | None:
    """Kill weekend or night shifts the Profile's ``shift_preference`` rules out.

    ``no_preference`` means the rule never fires. R1 loads the closed field;
    this is the deterministic Hard Filter that reads it (decision 11).
    """
    preference = policy.shift_preference
    if preference == "no_preference":
        return None
    title = str(posting.get("title") or "")
    description = strip_html(str(posting.get("description_html") or ""), preserve_blocks=True)
    text = f"{title}\n{description}"
    weekend_blob = text
    weekend = _first_shift_hit(_SHIFT_WEEKEND, text)
    if weekend is None:
        weekend = _first_shift_hit(_SHIFT_WEEKEND_TITLE, title)
        weekend_blob = title
    night = _first_shift_hit(_SHIFT_NIGHT, text)
    kill_weekend = preference in {"weekdays_only", "no_weekends"}
    kill_night = preference in {"weekdays_only", "no_nights"}
    if kill_weekend and weekend is not None:
        return Reading(
            "kill",
            "shift",
            f"shift preference {preference} excludes weekend work: {_evidence(weekend_blob, weekend)!r}",
        )
    if kill_night and night is not None:
        return Reading(
            "kill",
            "shift",
            f"shift preference {preference} excludes night shifts: {_evidence(text, night)!r}",
        )
    if _truncated(posting) and not weekend and not night:
        return Reading("pass", "shift", "shift preference could not be assessed: the stored description is a truncated snippet", "unassessed")
    return Reading("pass", "shift", f"no excluded shift for preference {preference}", "clear")


def eligibility_reading(posting: dict, policy: FilterPolicy = EMPTY_POLICY) -> Reading:
    """Apply explicit timing/pay preferences while preserving unknown facts."""
    search = policy.search
    finding = eligibility_finding(
        posting,
        search.timing,
        salary_floor=search.salary_floor,
        salary_currency=search.salary_currency,
        salary_period=search.salary_period,
    )
    if finding.status == "kill":
        return Reading("kill", "eligibility", finding.reason)
    return Reading("pass", "eligibility", finding.reason, finding.fact)


HARD_FILTER_RULES: dict[str, HardFilter] = {
    "work_authorization": work_authorization_reading,
    "education_fit": education_fit_reading,
    "role_target": role_target_reading,
    "eligibility": eligibility_reading,
}


def hard_filters(policy: FilterPolicy = EMPTY_POLICY, *, jev: JevContext | None = None) -> tuple[HardFilter, ...]:
    """The Hard Filters this Profile enables, in the order it names them."""
    skip_description = policy.qualification_mode == "jev" and jev is not None
    return tuple(
        HARD_FILTER_RULES[rule]
        for rule in policy.enabled
        if not skip_description or rule not in DESCRIPTION_ONLY_RULES
    )


def _jev_reserved(jev: JevContext) -> dict[str, str]:
    return {"jev": jev.assessment_key, "jev_input": jev.input_version}


def _humanized(rules: tuple[str, ...]) -> str:
    names = [rule.replace("_", " ") for rule in rules]
    if len(names) > 2:
        return f"{', '.join(names[:-1])}, and {names[-1]}"
    return " and ".join(names)


def apply_filters(
    posting: dict,
    policy: FilterPolicy = EMPTY_POLICY,
    *,
    jev: JevContext | None = None,
) -> Decision:
    """Apply the Profile's Hard Filters in its order and return the first kill.

    ``jev`` is the current promoted Jev context. Supplying one outside jev
    mode is a caller error. In jev mode ``None`` is complete deterministic
    fallback, not provisional delegation. A valid context delegates exactly
    ``work_authorization`` and ``education_fit``; operational kills still
    run, then a Jev ``exclude`` becomes ``rule='jev_policy:<primary_rule>'``.
    ``review`` and ``prioritize`` do not mean operationally suitable.

    On a jev-mode kill the facts object holds only the reserved ``jev`` /
    ``jev_input`` pair. On a pass it also holds the rule readings. Fallback
    (no context) leaves those two tokens for ``match.run`` to stamp from the
    resolution, because this function does not see the current input version.
    """
    if jev is not None and policy.qualification_mode != "jev":
        raise ValueError("a Jev context is only valid when qualification_mode is jev")
    jev_mode = policy.qualification_mode == "jev"
    delegate = jev_mode and jev is not None

    def finish_kill(rule: str | None, reason: str) -> Decision:
        if jev_mode and jev is not None:
            return Decision("kill", rule, reason, _jev_reserved(jev))
        return Decision("kill", rule, reason)

    def finish_pass(reason: str, facts: dict[str, str]) -> Decision:
        if jev_mode and jev is not None:
            facts = {**facts, **_jev_reserved(jev)}
        return Decision("pass", None, reason, facts)

    facts: dict[str, str] = {}
    if delegate:
        for rule in DESCRIPTION_ONLY_RULES:
            facts[rule] = "delegated"
    for hard_filter in hard_filters(policy, jev=jev):
        verdict, rule, reason, fact = hard_filter(posting, policy)
        if verdict == "kill":
            return finish_kill(rule, reason)
        if rule is not None and fact is not None:
            facts[rule] = fact
    shift = shift_preference_reading(posting, policy)
    if shift is not None:
        if shift.verdict == "kill":
            return finish_kill(shift.rule, shift.reason)
        if shift.rule is not None and shift.fact is not None:
            facts[shift.rule] = shift.fact
    # Legacy Profiles did not list ``eligibility`` because it did not exist.
    # Rows that now carry an explicit deadline, cohort, or pay range still need
    # the deterministic check, while ordinary legacy rows retain their exact
    # old rule/fact shape.  Configured search timing/pay is auto-enabled by the
    # Profile loader and reaches the loop above.
    if "eligibility" not in policy.enabled and eligibility_relevant(
        posting,
        policy.search.timing,
        salary_floor=policy.search.salary_floor,
    ):
        reading = eligibility_reading(posting, policy)
        if reading.verdict == "kill":
            return finish_kill(reading.rule, reading.reason)
        if reading.rule is not None and reading.fact is not None:
            facts[reading.rule] = reading.fact
    if delegate and jev is not None and jev.decision == "exclude":
        primary = jev.primary_rule or "exclude"
        return finish_kill(
            f"jev_policy:{primary}",
            f"Jev policy exclusion ({primary})",
        )
    if not policy.enabled:
        parts: list[str] = []
        if shift is not None:
            parts.append(shift.reason)
        if parts or facts:
            reason = "; ".join(parts) if parts else "no Hard Filter is configured; passed"
            if delegate and jev is not None:
                reason += f"; qualification delegated to Jev {jev.qualifier_version}"
            return finish_pass(reason, facts)
        return finish_pass("no Hard Filter is configured; passed", facts)
    skip_description = delegate
    effective_enabled = tuple(
        rule for rule in policy.enabled
        if not skip_description or rule not in DESCRIPTION_ONLY_RULES
    )
    if "eligibility" in facts and "eligibility" not in effective_enabled:
        effective_enabled = (*effective_enabled, "eligibility")
    if "shift" in facts and "shift" not in effective_enabled:
        effective_enabled = (*effective_enabled, "shift")
    reason = (f"passed {_humanized(effective_enabled)} hard filters" if effective_enabled else
              "no deterministic Hard Filter is configured; passed")
    if delegate and jev is not None:
        reason = f"{reason}; qualification delegated to Jev {jev.qualifier_version}"
    if shift is not None:
        reason += f"; {shift.reason}"
    # The aggregate reason is what the dashboard shows, so it cannot claim a
    # Posting survived filters that never had the text to run on.
    blind = tuple(rule for rule in DESCRIPTION_ONLY_RULES if rule in policy.enabled)
    if blind and _truncated(posting) and not delegate:
        return finish_pass(
            f"{reason}, but {_humanized(blind)} could not be assessed: {TRUNCATED_NOTE}",
            facts,
        )
    return finish_pass(reason, facts)


# The three-value view of each rule, which is the shape every caller and every
# test outside this module asks for. A rule's verdict, rule name and reason are
# unchanged by this file's facts, and are now *unchangeable* by them: the fact is
# not in the value these return. That is what makes "recording only, nothing
# decides differently" a property of the code rather than a promise in a commit
# message — and it is why ``tests/match/test_filters.py`` still exercises the
# whole rule surface without a single line of it moving.


def work_authorization_filter(posting: dict, policy: FilterPolicy = EMPTY_POLICY) -> FilterResult:
    """``work_authorization_reading``'s verdict, rule and reason, without its fact."""
    return work_authorization_reading(posting, policy).decision


def education_fit_filter(posting: dict, policy: FilterPolicy = EMPTY_POLICY) -> FilterResult:
    """``education_fit_reading``'s verdict, rule and reason, without its fact."""
    return education_fit_reading(posting, policy).decision


def role_target_filter(posting: dict, policy: FilterPolicy = EMPTY_POLICY) -> FilterResult:
    """``role_target_reading``'s verdict, rule and reason, without its fact."""
    return role_target_reading(posting, policy).decision
