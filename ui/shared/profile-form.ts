/**
 * The Profile screen's form: what an existing Profile looks like as fields, and what holds
 * a save.
 *
 * Server and screen compile against the same shape, the way `onboarding.ts` works for setup.
 * `GET /api/onboarding/existing-profile` answers with one of these read out of the three
 * files, and `POST` takes one back. **It is a view of the files, not a second Profile
 * schema.** Everything the form does not name — the Jev policy, `qualification_mode`, the
 * wording patterns, `timing`, `option_aliases`, `profile.id`, an entry's `primary` or
 * `include` flag — stays in the file untouched, because the server patches what changed
 * rather than re-emitting what it read (`server/onboarding/profile-form.ts`).
 *
 * Two words are load-bearing:
 *
 * - `origin` on a résumé entry is its index in the file the person opened. The server uses it
 *   to patch that entry in place, so the flags the form does not show survive; a new entry
 *   has `origin: null` and is appended.
 * - A `TriState` is the form of a YAML `true`, `false` or `null`. It is never a boolean,
 *   because `null` is the answer a form field is never filled from, and a screen has to be
 *   able to say "not answered" rather than defaulting anybody to No.
 */

/** `schema.py: DEGREE_LEVELS`, lowest first. */
export const DEGREE_LEVELS = ["high_school", "associate", "bachelor", "master", "doctorate"] as const;

export type DegreeLevel = (typeof DEGREE_LEVELS)[number];

/** `loader.py: KNOWN_RULES` — the Hard Filters a Profile may enable, in the order they show. */
export const HARD_FILTER_RULES = ["work_authorization", "education_fit", "role_target", "eligibility"] as const;

export type HardFilterRule = (typeof HARD_FILTER_RULES)[number];

/** `schema.py: REMOTE_PREFERENCES`. */
export const REMOTE_PREFERENCES = ["required", "preferred", "acceptable", "no"] as const;

export type RemotePreference = (typeof REMOTE_PREFERENCES)[number];

/** A YAML `true`, `false` or `null`, as a form shows it. */
export type TriState = "unanswered" | "yes" | "no";

export const TRI_STATES: readonly TriState[] = ["unanswered", "yes", "no"];

export type ResumeContactForm = {
	readonly location: string;
	readonly phone: string;
	readonly email: string;
	readonly linkedin: string;
};

export type EducationForm = {
	readonly origin: number | null;
	readonly org: string;
	readonly location: string;
	readonly date: string;
	readonly degree: string;
	readonly gpa: string;
	readonly coursework: string;
};

export type ProficiencyForm = {
	readonly origin: number | null;
	readonly label: string;
	readonly items: string;
};

export type ExperienceForm = {
	readonly origin: number | null;
	readonly org: string;
	readonly role: string;
	readonly location: string;
	readonly dates: string;
	readonly bullets: readonly string[];
};

export type ResumeForm = {
	readonly name: string;
	readonly contact: ResumeContactForm;
	readonly education: readonly EducationForm[];
	readonly technical_proficiencies: readonly ProficiencyForm[];
	readonly experience: readonly ExperienceForm[];
};

/** The six screening answers and the four EEO answers `constraints.yaml` documents. */
export const SCREENING_FIELDS = [
	"salary_expectations",
	"resides_near_posting",
	"prior_employment_at_company",
	"relatives_at_company",
	"how_heard",
	"country",
] as const;

export type ScreeningField = (typeof SCREENING_FIELDS)[number];

export const EEO_FIELDS = ["gender", "hispanic_latino", "veteran_status", "disability_status"] as const;

export type EeoField = (typeof EEO_FIELDS)[number];

export type ConstraintsForm = {
	readonly work_authorization: {
		readonly status: string;
		readonly requires_sponsorship: TriState;
		readonly authorized_to_work: TriState;
	};
	readonly screening: Readonly<Record<ScreeningField, string>> & {
		readonly eeo: Readonly<Record<EeoField, string>>;
	};
};

/** One board the Profile polls, with the employer's name from `sources.names` or blank. */
export type EmployerForm = {
	readonly source: string;
	readonly board: string;
	readonly name: string;
};

export type TargetingForm = {
	readonly profileName: string;
	readonly search: {
		readonly queries: readonly string[];
		readonly locations: readonly string[];
		readonly remote: RemotePreference | null;
	};
	readonly employers: readonly EmployerForm[];
	readonly filters: {
		readonly enabled: readonly string[];
		/** Which rule blocks the file writes, read from it and never edited. */
		readonly configured: readonly string[];
		readonly role_target: {
			/** The ladder's rung names, lowest first. Read from the file and never edited. */
			readonly levels: readonly string[];
			readonly accept: readonly string[];
			readonly exclude_terms: readonly string[];
		};
		readonly education_fit: {
			readonly holds: DegreeLevel | null;
			readonly in_progress: DegreeLevel | null;
			readonly experience_years_kill: number | null;
			/** The ceiling the loader reads, or its default. Read from the file and never edited. */
			readonly experience_years_implausible: number;
		};
	};
};

export type ProfileForm = {
	readonly resume: ResumeForm;
	readonly constraints: ConstraintsForm;
	readonly targeting: TargetingForm;
};

/** The three file strings as the person opened them. The stale check compares these. */
export type ProfileDocuments = Record<"resume.yaml" | "constraints.yaml" | "targeting.yaml", string>;

export type ExistingProfileResponse = {
	readonly documents: ProfileDocuments;
	readonly form: ProfileForm;
};

/** One thing holding a save, and which field it sits beside. */
export type FormIssue = {
	readonly field: string;
	readonly message: string;
};

export function isDegreeLevel(value: string): value is DegreeLevel {
	return DEGREE_LEVELS.some((level) => level === value);
}

export function isRemotePreference(value: string): value is RemotePreference {
	return REMOTE_PREFERENCES.some((preference) => preference === value);
}

export function isTriState(value: string): value is TriState {
	return TRI_STATES.some((state) => state === value);
}

export function isHardFilterRule(value: string): value is HardFilterRule {
	return HARD_FILTER_RULES.some((rule) => rule === value);
}

function blank(value: string): boolean {
	return value.trim() === "";
}

function resumeHasContent(resume: ResumeForm): boolean {
	const contact = resume.contact;
	return (
		!blank(resume.name) ||
		!blank(contact.location) || !blank(contact.phone) || !blank(contact.email) || !blank(contact.linkedin) ||
		resume.education.length > 0 || resume.technical_proficiencies.length > 0 || resume.experience.length > 0
	);
}

/**
 * What the loader — and the renderer behind it — would refuse, said beside the field.
 *
 * Each rule is one the Python side already states: `loader.py` for the Hard Filters and the
 * board registry, `resume/render.py` for the five contact facts. The screen holds Save on
 * the first of these; the server runs the same function over the patched files and refuses
 * on it too, before the load-back ever runs. Whatever neither of them thought of, the
 * load-back still catches.
 */
export function validateProfileForm(form: ProfileForm): readonly FormIssue[] {
	const issues: FormIssue[] = [];
	const { resume, targeting } = form;
	if (resumeHasContent(resume)) {
		if (blank(resume.name)) issues.push({ field: "resume.name", message: "A résumé needs a name." });
		for (const field of ["location", "email", "phone", "linkedin"] as const) {
			if (blank(resume.contact[field])) {
				issues.push({ field: `resume.contact.${field}`, message: "Every contact line is printed on the résumé, so it can’t be blank." });
			}
		}
	}
	const { filters, employers } = targeting;
	for (const rule of filters.enabled) {
		if (!isHardFilterRule(rule)) issues.push({ field: "targeting.filters.enabled", message: "That is not a Hard Filter this pipeline knows." });
	}
	const fit = filters.education_fit;
	const configured = new Set(filters.configured);
	if (fit.holds !== null || fit.in_progress !== null || fit.experience_years_kill !== null) configured.add("education_fit");
	if (filters.role_target.levels.length > 0 || filters.role_target.accept.length > 0 || filters.role_target.exclude_terms.length > 0) {
		configured.add("role_target");
	}
	if (configured.size > 0 && filters.enabled.length === 0) {
		issues.push({ field: "targeting.filters.enabled", message: "Turn on at least one Hard Filter, or every job would pass unchecked." });
	}
	if (fit.experience_years_kill !== null && (!Number.isInteger(fit.experience_years_kill) || fit.experience_years_kill < 0)) {
		issues.push({ field: "targeting.filters.education_fit.experience_years_kill", message: "Years is a whole number." });
	} else if (fit.experience_years_kill !== null && fit.experience_years_kill > fit.experience_years_implausible) {
		issues.push({
			field: "targeting.filters.education_fit.experience_years_kill",
			message: `Above ${String(fit.experience_years_implausible)} years a requirement reads as company history, so the limit can’t go higher than that.`,
		});
	}
	if (fit.holds !== null && fit.in_progress !== null && DEGREE_LEVELS.indexOf(fit.in_progress) < DEGREE_LEVELS.indexOf(fit.holds)) {
		issues.push({ field: "targeting.filters.education_fit.in_progress", message: "A degree in progress can’t be below the one you hold." });
	}
	const { levels, accept } = filters.role_target;
	if (levels.length > 0 && accept.length === 0) {
		issues.push({ field: "targeting.filters.role_target.accept", message: "Choose at least one level, or every job would be excluded." });
	}
	for (const rung of accept) {
		if (!levels.includes(rung)) issues.push({ field: "targeting.filters.role_target.accept", message: "That level is not on this Profile’s ladder." });
	}
	if (employers.some((employer) => !blank(employer.name)) && employers.some((employer) => blank(employer.name))) {
		issues.push({ field: "targeting.employers", message: "Name every employer, or none: an unnamed board beside named ones can’t be loaded." });
	}
	return issues;
}

/** The first issue on one field, or null. What a row shows under itself. */
export function issueAt(issues: readonly FormIssue[], field: string): string | null {
	return issues.find((issue) => issue.field === field)?.message ?? null;
}
