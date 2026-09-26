import { Minus } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import type { BoardEntry, DiscoveredProfile, OnboardingStateResponse } from "../../shared/onboarding.ts";
import {
	DEGREE_LEVELS,
	EEO_FIELDS,
	HARD_FILTER_RULES,
	isDegreeLevel,
	isRemotePreference,
	isTriState,
	issueAt,
	SCREENING_FIELDS,
	TRI_STATES,
	validateProfileForm,
	type EducationForm,
	type EmployerForm,
	type ExperienceForm,
	type FormIssue,
	type ProficiencyForm,
	type ProfileDocuments,
	type ProfileForm,
	type TargetingForm,
} from "../../shared/profile-form.ts";
import { Sheet } from "../components/sheet.tsx";
import { Toolbar } from "../components/toolbar.tsx";
import { assistantLabel, degreeLevelLabel, eeoFieldLabel, extractedFieldLabel, hardFilterLine, ruleLabel, rungLabel, screeningFieldLabel, triStateLabel } from "../labels.ts";
import { chooseDocumentRuntime, OnboardingRequestError, readInstallSettings, readProfileForm, saveProfileForm, type InstallSettings } from "../onboarding/api.ts";
import { lines, REMOTE_OPTIONS, type EducationEntry, type ExperienceEntry, type ProficiencyEntry } from "../onboarding/draft.ts";
import { CONNECT, PROFILE, RESUME, TARGETING } from "./onboarding/copy.ts";
import { EmployerPicker, boardKey } from "./onboarding/employers.tsx";
import { EDUCATION_FIELDS, EntryFields, EXPERIENCE_FIELDS, JevKeyField, Popup, PROFICIENCY_FIELDS, Row, rowWords, skillsOf, TokenField } from "./onboarding/parts.tsx";

/** The Contact group's order: the résumé step's, so the two screens read alike. */
const CONTACT_ORDER = ["name", "location", "email", "phone", "linkedin"] as const;

type Loaded = { readonly form: ProfileForm; readonly documents: ProfileDocuments };

type Refusal = { readonly message: string; readonly remedy: string | null; readonly field: string | null };

type Editing =
	| { readonly kind: "experience"; readonly index: number; readonly entry: ExperienceEntry }
	| { readonly kind: "education"; readonly index: number; readonly entry: EducationEntry }
	| { readonly kind: "skills"; readonly entries: readonly ProficiencyForm[] }
	| null;

function experienceEntry(entry: ExperienceForm): ExperienceEntry {
	return { org: entry.org, role: entry.role, location: entry.location, dates: entry.dates, bullets: entry.bullets.join("\n") };
}

function educationEntry(entry: EducationForm): EducationEntry {
	const { org, location, date, degree, gpa, coursework } = entry;
	return { org, location, date, degree, gpa, coursework };
}

const NEW_EXPERIENCE: ExperienceEntry = { org: "", role: "", location: "", dates: "", bullets: "" };
const NEW_EDUCATION: EducationEntry = { org: "", location: "", date: "", degree: "", gpa: "", coursework: "" };

function blankEntry(entry: Readonly<Record<string, string>>): boolean {
	return Object.values(entry).every((value) => value.trim() === "");
}

/** One row of a group: a label on the left, the field on the right, and what holds it under the field. */
function FieldRow({ label, issue, children }: { readonly label: string; readonly issue: string | null; readonly children: ReactNode }) {
	return (
		<label className="setup-row">
			<span className="setup-row-label">{label}</span>
			<span className="field-lines">
				{children}
				{issue === null ? null : <span className="field-issue" role="alert">{issue}</span>}
			</span>
		</label>
	);
}

/** Every contiguous band of the Profile's own ladder, as the Level popup offers them. */
function bandOptions(levels: readonly string[]): readonly { readonly value: string; readonly label: string; readonly accept: readonly string[] }[] {
	return levels.flatMap((low, from) =>
		levels.slice(from).map((high, offset) => ({
			value: `${String(from)}-${String(from + offset)}`,
			label: offset === 0 ? rungLabel(low) : `${rungLabel(low)} to ${rungLabel(high)}`,
			accept: levels.slice(from, from + offset + 1),
		})),
	);
}

function sameSet(left: readonly string[], right: readonly string[]): boolean {
	return left.length === right.length && left.every((value) => right.includes(value));
}

function withTargeting(form: ProfileForm, targeting: Partial<TargetingForm>): ProfileForm {
	return { ...form, targeting: { ...form.targeting, ...targeting } };
}

function withFilters(form: ProfileForm, filters: Partial<TargetingForm["filters"]>): ProfileForm {
	return withTargeting(form, { filters: { ...form.targeting.filters, ...filters } });
}

/**
 * The Profile screen: an existing Profile as fields, and one Save.
 *
 * What is shown is `formOf` the three files, read by the server; what Save sends is the form
 * and the files as they were opened, and the server writes only the difference into them
 * (`server/onboarding/profile-form.ts`). Everything the form does not show — the Jev policy,
 * the wording patterns, an entry's flags — is not on this screen and is not touched by it.
 *
 * Save is held, with the reason beside it, while a field would be refused; a refusal the
 * server names a field for lands under that field, and one it does not lands at the top. The
 * assistant and the Jev key are this Mac's settings, saved the moment they change, and are
 * never part of Save.
 */
export function ProfileView({ name, chooser, onSaved, sidebarHidden, onShowSidebar }: {
	readonly name: string;
	/** The Profile chooser, when this Install holds more than one; sits in the toolbar. */
	readonly chooser: ReactNode;
	readonly onSaved: () => void;
	readonly sidebarHidden: boolean;
	readonly onShowSidebar: () => void;
}) {
	const [loaded, setLoaded] = useState<Loaded | null>(null);
	const [form, setForm] = useState<ProfileForm | null>(null);
	const [checked, setChecked] = useState<ReadonlyMap<string, boolean>>(new Map());
	const [settings, setSettings] = useState<InstallSettings | null>(null);
	const [refusal, setRefusal] = useState<Refusal | null>(null);
	const [notice, setNotice] = useState<string | null>(null);
	const [saving, setSaving] = useState(false);
	const [editing, setEditing] = useState<Editing>(null);

	useEffect(() => {
		let active = true;
		setLoaded(null);
		setForm(null);
		setRefusal(null);
		setNotice(null);
		readProfileForm(name)
			.then((response) => {
				if (!active) return;
				setLoaded({ form: response.form, documents: response.documents });
				setForm(response.form);
			})
			.catch((error: Error) => {
				if (active) setRefusal(error instanceof OnboardingRequestError ? { message: error.message, remedy: error.remedy, field: null } : { message: PROFILE.notOpened, remedy: null, field: null });
			});
		readInstallSettings()
			.then((install) => {
				if (active) setSettings(install);
			})
			.catch(() => {
				if (active) setSettings(null);
			});
		return () => {
			active = false;
		};
	}, [name]);

	const edit = useCallback((next: ProfileForm) => {
		setForm(next);
		setRefusal(null);
		setNotice(null);
	}, []);

	/** What holds Save: a refusal the server named a field for, then the form's own checks. */
	const issues = useMemo<readonly FormIssue[]>(() => {
		const own = form === null ? [] : validateProfileForm(form);
		return refusal === null || refusal.field === null ? own : [{ field: refusal.field, message: refusal.message }, ...own];
	}, [form, refusal]);
	const dirty = loaded !== null && form !== null && JSON.stringify(form) !== JSON.stringify(loaded.form);
	const hold = saving ? null : issues[0]?.message ?? (dirty ? null : PROFILE.unchanged);

	const save = () => {
		if (loaded === null || form === null || saving || hold !== null) return;
		setSaving(true);
		setRefusal(null);
		setNotice(null);
		saveProfileForm(name, form, loaded.documents)
			.then(() => readProfileForm(name))
			.then((response) => {
				setLoaded({ form: response.form, documents: response.documents });
				setForm(response.form);
				setNotice(PROFILE.saved);
				onSaved();
			})
			.catch((error: Error) => {
				setRefusal(error instanceof OnboardingRequestError ? { message: error.message, remedy: error.remedy, field: error.field } : { message: error.message, remedy: null, field: null });
			})
			.finally(() => setSaving(false));
	};

	const toolbar = (
		<Toolbar
			title={PROFILE.title}
			subtitle={name}
			search={null}
			sidebarHidden={sidebarHidden}
			onShowSidebar={onShowSidebar}
			layout="single"
			actions={
				<>
					{chooser}
					{hold === null ? null : <span className="toolbar-note">{hold}</span>}
					<button type="button" className="capsule" disabled={saving || hold !== null} onClick={save}>
						{saving ? PROFILE.saving : PROFILE.save}
					</button>
				</>
			}
		/>
	);

	if (form === null || loaded === null) {
		return (
			<>
				{toolbar}
				<div className="setup-body">
					<div className="setup-column" data-density="tight" data-inset="toolbar">
						{refusal === null ? <p className="setup-caption" role="status">{PROFILE.loading}</p> : (
							<div className="setup-alert" role="alert">
								<p>{refusal.message}</p>
								{refusal.remedy === null ? null : <p>{refusal.remedy}</p>}
							</div>
						)}
					</div>
				</div>
			</>
		);
	}

	const { resume, constraints, targeting } = form;
	const issue = (field: string) => issueAt(issues, field);
	const setResume = (next: Partial<ProfileForm["resume"]>) => edit({ ...form, resume: { ...resume, ...next } });
	const setConstraints = (next: Partial<ProfileForm["constraints"]>) => edit({ ...form, constraints: { ...constraints, ...next } });

	const commitEditing = () => {
		if (editing === null) return;
		if (editing.kind === "experience") {
			const entry = editing.entry;
			const stated: ExperienceForm = { origin: resume.experience[editing.index]?.origin ?? null, org: entry.org, role: entry.role, location: entry.location, dates: entry.dates, bullets: lines(entry.bullets) };
			const experience = editing.index < resume.experience.length ? resume.experience.map((old, at) => (at === editing.index ? stated : old)) : [...resume.experience, stated];
			setResume({ experience: blankEntry(entry) ? experience.filter((_, at) => at !== editing.index) : experience });
		} else if (editing.kind === "education") {
			const entry = editing.entry;
			const stated: EducationForm = { origin: resume.education[editing.index]?.origin ?? null, ...entry };
			const education = editing.index < resume.education.length ? resume.education.map((old, at) => (at === editing.index ? stated : old)) : [...resume.education, stated];
			setResume({ education: blankEntry(entry) ? education.filter((_, at) => at !== editing.index) : education });
		} else {
			setResume({ technical_proficiencies: editing.entries.filter((entry) => entry.label.trim() !== "" || entry.items.trim() !== "") });
		}
		setEditing(null);
	};
	const removeEditing = () => {
		if (editing?.kind === "experience") setResume({ experience: resume.experience.filter((_, at) => at !== editing.index) });
		if (editing?.kind === "education") setResume({ education: resume.education.filter((_, at) => at !== editing.index) });
		setEditing(null);
	};

	const boards: readonly BoardEntry[] = targeting.employers.map((employer) => ({ ...employer, confirmed: checked.get(boardKey(employer)) ?? null }));
	const setBoards = (next: readonly BoardEntry[]) => {
		setChecked(new Map(next.flatMap((board) => (board.confirmed === null ? [] : [[boardKey(board), board.confirmed] as const]))));
		edit(withTargeting(form, { employers: next.map((board): EmployerForm => ({ source: board.source, board: board.board, name: board.name })) }));
	};

	const ladder = targeting.filters.role_target.levels;
	const bands = bandOptions(ladder);
	const band = bands.find((option) => sameSet(option.accept, targeting.filters.role_target.accept));
	const fit = targeting.filters.education_fit;
	const setFit = (next: Partial<TargetingForm["filters"]["education_fit"]>) => edit(withFilters(form, { education_fit: { ...fit, ...next } }));
	const setRoleTarget = (next: Partial<TargetingForm["filters"]["role_target"]>) => edit(withFilters(form, { role_target: { ...targeting.filters.role_target, ...next } }));

	let sheet: ReactNode = null;
	if (editing?.kind === "experience") {
		sheet = <EntryFields section="experience" entry={editing.entry} fields={EXPERIENCE_FIELDS} onEntry={(entry) => setEditing({ ...editing, entry })} />;
	} else if (editing?.kind === "education") {
		sheet = <EntryFields section="education" entry={editing.entry} fields={EDUCATION_FIELDS} onEntry={(entry) => setEditing({ ...editing, entry })} />;
	} else if (editing?.kind === "skills") {
		sheet = (
			<>
				{editing.entries.map((entry, index) => (
					<div key={String(index)} className="sheet-entry">
						<EntryFields
							section="technical_proficiencies"
							entry={{ label: entry.label, items: entry.items }}
							fields={PROFICIENCY_FIELDS}
							onEntry={(next) => setEditing({ ...editing, entries: editing.entries.map((old, at) => (at === index ? { ...old, ...next } : old)) })}
						/>
						<button type="button" className="icon-button" aria-label={`${PROFILE.remove} ${entry.label}`} title={PROFILE.remove} onClick={() => setEditing({ ...editing, entries: editing.entries.filter((_, at) => at !== index) })}>
							<Minus aria-hidden="true" className="icon" />
						</button>
					</div>
				))}
				<div>
					<button type="button" className="button" data-size="large" onClick={() => setEditing({ ...editing, entries: [...editing.entries, { origin: null, label: "", items: "" }] })}>
						{PROFILE.addSkills}
					</button>
				</div>
			</>
		);
	}

	return (
		<>
			{toolbar}
			<div className="setup-body">
				<div className="setup-column" data-density="tight" data-inset="toolbar">
					{refusal !== null && refusal.field === null ? (
						<div className="setup-alert" role="alert">
							<p>{refusal.message}</p>
							{refusal.remedy === null ? null : <p>{refusal.remedy}</p>}
						</div>
					) : null}
					{notice === null ? null : <p className="setup-caption" role="status">{notice}</p>}

					<section className="setup-section" aria-label={PROFILE.contact}>
						<h2 className="group-header">{PROFILE.contact}</h2>
						<div className="setup-group">
							{CONTACT_ORDER.map((field) => (
								<FieldRow key={field} label={extractedFieldLabel(field)} issue={issue(field === "name" ? "resume.name" : `resume.contact.${field}`)}>
									<input
										className="text-field"
										value={field === "name" ? resume.name : resume.contact[field]}
										onChange={(event) => (field === "name" ? setResume({ name: event.target.value }) : setResume({ contact: { ...resume.contact, [field]: event.target.value } }))}
									/>
								</FieldRow>
							))}
						</div>
					</section>

					<section className="setup-section" aria-label={PROFILE.history}>
						<h2 className="group-header">{PROFILE.history}</h2>
						<div className="setup-group">
							{resume.experience.map((entry, index) => (
								<Row key={`experience-${String(index)}`} {...rowWords(entry.role, entry.org, entry.dates)} onOpen={() => setEditing({ kind: "experience", index, entry: experienceEntry(entry) })} />
							))}
							{resume.education.map((entry, index) => (
								<Row key={`education-${String(index)}`} {...rowWords(entry.degree, entry.org, entry.date)} onOpen={() => setEditing({ kind: "education", index, entry: educationEntry(entry) })} />
							))}
							<div className="setup-row">
								<button type="button" className="button" data-size="large" onClick={() => setEditing({ kind: "experience", index: resume.experience.length, entry: NEW_EXPERIENCE })}>
									{PROFILE.addExperience}
								</button>
								<button type="button" className="button" data-size="large" onClick={() => setEditing({ kind: "education", index: resume.education.length, entry: NEW_EDUCATION })}>
									{PROFILE.addEducation}
								</button>
							</div>
						</div>
					</section>

					<section className="setup-section" aria-label={PROFILE.skills}>
						<h2 className="group-header">{PROFILE.skills}</h2>
						<button type="button" className="chip-list setup-group" aria-label={RESUME.editSkills} onClick={() => setEditing({ kind: "skills", entries: resume.technical_proficiencies })}>
							{skillsOf(resume.technical_proficiencies.map((entry): ProficiencyEntry => ({ label: entry.label, items: entry.items }))).map((skill, index) => (
								<span key={`${skill}-${String(index)}`} className="chip-token">
									{skill}
								</span>
							))}
							{resume.technical_proficiencies.length === 0 ? <span className="setup-row-subtitle">{PROFILE.addSkills}</span> : null}
						</button>
					</section>

					<section className="setup-section" aria-label={PROFILE.jobs}>
						<h2 className="group-header">{PROFILE.jobs}</h2>
						<div className="setup-group">
							<div className="setup-row">
								<span className="setup-row-label">{TARGETING.titles}</span>
								<TokenField label={TARGETING.titles} tokens={targeting.search.queries} placeholder={TARGETING.addTitle} onChange={(queries) => edit(withTargeting(form, { search: { ...targeting.search, queries } }))} />
							</div>
							{ladder.length === 0 ? null : (
								<FieldRow label={TARGETING.level} issue={issue("targeting.filters.role_target.accept")}>
									<Popup
										label={TARGETING.level}
										value={band?.value ?? "as-written"}
										onChange={(value) => {
											const chosen = bands.find((option) => option.value === value);
											if (chosen !== undefined) setRoleTarget({ accept: chosen.accept });
										}}
									>
										{band === undefined ? <option value="as-written">{PROFILE.levelAsWritten}</option> : null}
										{bands.map((option) => (
											<option key={option.value} value={option.value}>
												{option.label}
											</option>
										))}
									</Popup>
								</FieldRow>
							)}
							<div className="setup-row">
								<span className="setup-row-label">{TARGETING.places}</span>
								<TokenField label={TARGETING.places} tokens={targeting.search.locations} placeholder={TARGETING.addPlace} onChange={(locations) => edit(withTargeting(form, { search: { ...targeting.search, locations } }))} />
							</div>
							<div className="setup-row">
								<span className="setup-row-label">{TARGETING.remote}</span>
								<Popup
									label={TARGETING.remote}
									value={targeting.search.remote ?? ""}
									onChange={(value) => edit(withTargeting(form, { search: { ...targeting.search, remote: isRemotePreference(value) ? value : null } }))}
								>
									<option value="">{PROFILE.remoteUnset}</option>
									{REMOTE_OPTIONS.map((option) => (
										<option key={option.value} value={option.value}>
											{option.label}
										</option>
									))}
								</Popup>
							</div>
						</div>
					</section>

					<EmployerPicker boards={boards} onChange={setBoards} />
					{issue("targeting.employers") === null ? null : <p className="field-issue" role="alert">{issue("targeting.employers")}</p>}

					<section className="setup-section" aria-label={PROFILE.filters}>
						<h2 className="group-header">{PROFILE.filters}</h2>
						<div className="setup-group">
							{HARD_FILTER_RULES.map((rule) => (
								<label key={rule} className="setup-row">
									<span className="setup-row-lines">
										<span className="setup-row-title">{ruleLabel(rule)}</span>
										<span className="setup-row-subtitle">{hardFilterLine(rule)}</span>
									</span>
									<span className="form-check">
										<input
											type="checkbox"
											checked={targeting.filters.enabled.includes(rule)}
											aria-label={ruleLabel(rule)}
											onChange={(event) => {
												const enabled = event.target.checked
													? [...targeting.filters.enabled.filter((name) => name !== rule), rule]
													: targeting.filters.enabled.filter((name) => name !== rule);
												edit(withFilters(form, { enabled }));
											}}
										/>
									</span>
								</label>
							))}
						</div>
						{issue("targeting.filters.enabled") === null ? <p className="setup-caption">{PROFILE.filtersLine}</p> : <p className="field-issue" role="alert">{issue("targeting.filters.enabled")}</p>}
					</section>

					<section className="setup-section" aria-label={PROFILE.education}>
						<h2 className="group-header">{PROFILE.education}</h2>
						<div className="setup-group" data-labels="wide">
							<FieldRow label={PROFILE.holds} issue={issue("targeting.filters.education_fit.holds")}>
								<Popup label={PROFILE.holds} value={fit.holds ?? ""} onChange={(value) => setFit({ holds: isDegreeLevel(value) ? value : null })}>
									<option value="">{PROFILE.degreeUnset}</option>
									{DEGREE_LEVELS.map((level) => (
										<option key={level} value={level}>
											{degreeLevelLabel(level)}
										</option>
									))}
								</Popup>
							</FieldRow>
							<FieldRow label={PROFILE.inProgress} issue={issue("targeting.filters.education_fit.in_progress")}>
								<Popup label={PROFILE.inProgress} value={fit.in_progress ?? ""} onChange={(value) => setFit({ in_progress: isDegreeLevel(value) ? value : null })}>
									<option value="">{PROFILE.degreeUnset}</option>
									{DEGREE_LEVELS.map((level) => (
										<option key={level} value={level}>
											{degreeLevelLabel(level)}
										</option>
									))}
								</Popup>
							</FieldRow>
							<FieldRow label={PROFILE.yearsKill} issue={issue("targeting.filters.education_fit.experience_years_kill")}>
								<input
									className="text-field"
									type="number"
									min={0}
									step={1}
									inputMode="numeric"
									placeholder={PROFILE.yearsUnset}
									value={fit.experience_years_kill ?? ""}
									onChange={(event) => setFit({ experience_years_kill: event.target.value.trim() === "" ? null : Number(event.target.value) })}
								/>
							</FieldRow>
							<div className="setup-row">
								<span className="setup-row-label">{PROFILE.rolesToSkip}</span>
								<TokenField label={PROFILE.rolesToSkip} tokens={targeting.filters.role_target.exclude_terms} placeholder={PROFILE.addRole} onChange={(exclude_terms) => setRoleTarget({ exclude_terms })} />
							</div>
						</div>
						<p className="setup-caption">{PROFILE.yearsLine}</p>
					</section>

					<section className="setup-section" aria-label={PROFILE.answers}>
						<h2 className="group-header">{PROFILE.answers}</h2>
						<div className="setup-group" data-labels="wide">
							<FieldRow label={PROFILE.status} issue={issue("constraints.work_authorization.status")}>
								<input className="text-field" value={constraints.work_authorization.status} onChange={(event) => setConstraints({ work_authorization: { ...constraints.work_authorization, status: event.target.value } })} />
							</FieldRow>
							<div className="setup-row">
								<span className="setup-row-label">{PROFILE.sponsorship}</span>
								<Popup label={PROFILE.sponsorship} value={constraints.work_authorization.requires_sponsorship} onChange={(value) => setConstraints({ work_authorization: { ...constraints.work_authorization, requires_sponsorship: isTriState(value) ? value : "unanswered" } })}>
									{TRI_STATES.map((state) => (
										<option key={state} value={state}>
											{triStateLabel(state)}
										</option>
									))}
								</Popup>
							</div>
							<div className="setup-row">
								<span className="setup-row-label">{PROFILE.authorized}</span>
								<Popup label={PROFILE.authorized} value={constraints.work_authorization.authorized_to_work} onChange={(value) => setConstraints({ work_authorization: { ...constraints.work_authorization, authorized_to_work: isTriState(value) ? value : "unanswered" } })}>
									{TRI_STATES.map((state) => (
										<option key={state} value={state}>
											{triStateLabel(state)}
										</option>
									))}
								</Popup>
							</div>
						</div>
						<p className="setup-caption">{PROFILE.authorizationLine}</p>
						<p className="setup-caption">{PROFILE.answersLine}</p>
						<div className="setup-group" data-labels="wide">
							{SCREENING_FIELDS.map((field) => (
								<FieldRow key={field} label={screeningFieldLabel(field)} issue={issue(`constraints.screening.${field}`)}>
									<input className="text-field" value={constraints.screening[field]} onChange={(event) => setConstraints({ screening: { ...constraints.screening, [field]: event.target.value } })} />
								</FieldRow>
							))}
						</div>
						<h3 className="group-header">{PROFILE.eeo}</h3>
						<div className="setup-group" data-labels="wide">
							{EEO_FIELDS.map((field) => (
								<FieldRow key={field} label={eeoFieldLabel(field)} issue={issue(`constraints.screening.eeo.${field}`)}>
									<input className="text-field" value={constraints.screening.eeo[field]} onChange={(event) => setConstraints({ screening: { ...constraints.screening, eeo: { ...constraints.screening.eeo, [field]: event.target.value } } })} />
								</FieldRow>
							))}
						</div>
						<p className="setup-caption">{PROFILE.eeoLine}</p>
					</section>

					<section className="setup-section" aria-label={PROFILE.install}>
						<h2 className="group-header">{PROFILE.install}</h2>
						{settings === null ? null : (
							<>
								<div className="setup-group" data-labels="wide">
									<div className="setup-row">
										<span className="setup-row-label">{PROFILE.assistant}</span>
										<Popup
											label={PROFILE.assistant}
											value={settings.runtime}
											onChange={(value) => {
												const runtime = value === "codex" ? "codex" : "claude";
												chooseDocumentRuntime(runtime)
													.then(setSettings)
													.catch((error: Error) => setRefusal({ message: error.message, remedy: null, field: null }));
											}}
										>
											<option value="claude">{assistantLabel("claude")}</option>
											<option value="codex">{assistantLabel("codex")}</option>
										</Popup>
									</div>
									<div className="setup-row">
										<span className="setup-row-lines">
											<span className="setup-row-title">{CONNECT.jevKey}</span>
											<span className="setup-row-subtitle">{settings.jevKeyPresent ? CONNECT.keySaved : CONNECT.optional}</span>
										</span>
									</div>
								</div>
								<JevKeyField
									present={settings.jevKeyPresent}
									onSaved={() => {
										readInstallSettings().then(setSettings).catch(() => setSettings(null));
									}}
								/>
							</>
						)}
						<p className="setup-caption">{PROFILE.installLine}</p>
					</section>
				</div>
			</div>

			<Sheet
				open={editing !== null}
				onOpenChange={(open) => {
					if (!open) commitEditing();
				}}
				title={editing?.kind === "skills" ? PROFILE.skills : PROFILE.history}
				actions={
					<>
						{editing !== null && editing.kind !== "skills" ? (
							<button type="button" className="button" data-size="large" onClick={removeEditing}>
								{PROFILE.remove}
							</button>
						) : null}
						<button type="button" className="button" data-size="large" data-tone="prominent" onClick={commitEditing}>
							{PROFILE.done}
						</button>
					</>
				}
			>
				{sheet}
			</Sheet>
		</>
	);
}

/**
 * The Profile route: which Profile, and the screen for it.
 *
 * The implied Profile is the one the pipeline would run against; when this Install holds
 * more than one and implies none, the person chooses in the toolbar and nothing is guessed.
 */
export function ProfileRoute({ installState, selected, onSelect, onSaved, sidebarHidden, onShowSidebar }: {
	readonly installState: OnboardingStateResponse;
	readonly selected: string | null;
	readonly onSelect: (name: string | null) => void;
	readonly onSaved: () => void;
	readonly sidebarHidden: boolean;
	readonly onShowSidebar: () => void;
}) {
	const implied: DiscoveredProfile | null = installState.implied === null ? null : installState.profiles.find((profile) => profile.name === installState.implied) ?? null;
	const editable = installState.profiles.filter((profile) => profile.kind === "install" && !profile.scaffold);
	const name = implied?.kind === "install" ? implied.name : editable.some((profile) => profile.name === selected) ? selected : null;
	const chooser =
		implied === null && editable.length > 0 ? (
			<Popup label={PROFILE.choose} value={name ?? ""} onChange={(value) => onSelect(value === "" ? null : value)}>
				<option value="">{PROFILE.choose}</option>
				{editable.map((profile) => (
					<option key={profile.name} value={profile.name}>
						{profile.name}
					</option>
				))}
			</Popup>
		) : null;
	if (name === null) {
		return (
			<>
				<Toolbar title={PROFILE.title} subtitle={null} search={null} sidebarHidden={sidebarHidden} onShowSidebar={onShowSidebar} layout="single" actions={chooser} />
				<div className="setup-body">
					<div className="setup-column" data-density="tight" data-inset="toolbar">
						<div className="setup-alert" role="alert">
							<p>{editable.length === 0 ? PROFILE.noProfile : PROFILE.chooseFirst}</p>
						</div>
					</div>
				</div>
			</>
		);
	}
	return <ProfileView key={name} name={name} chooser={chooser} onSaved={onSaved} sidebarHidden={sidebarHidden} onShowSidebar={onShowSidebar} />;
}
