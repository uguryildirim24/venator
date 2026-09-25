import { ChevronRight, File as FileGlyph, FileText, LoaderCircle } from "lucide-react";
import { useCallback, useRef, useState, type DragEvent, type ReactNode } from "react";

import type { ExtractedField, ResumeParseMode } from "../../../shared/onboarding.ts";
import { Sheet, SheetClose } from "../../components/sheet.tsx";
import { assistantLabel, extractedFieldLabel, resumeFieldLabel } from "../../labels.ts";
import { importResumePdf, importResumeText, OnboardingRequestError } from "../../onboarding/api.ts";
import {
	unconfirmedFields,
	withParsedResume,
	withSuggestion,
	type EducationEntry,
	type ExperienceEntry,
	type ProficiencyEntry,
	type ResumeDraft,
	type SectionKey,
} from "../../onboarding/draft.ts";
import { readByLine, RESUME, THIS_MAC_CAPTION, wholeResumeCaption, wholeResumeOption } from "./copy.ts";
import { Popup } from "./parts.tsx";

/** Where the résumé on screen came from: a file or pasted text, and who read it. */
export type ResumeSource = {
	readonly name: string;
	/** The assistant that read it, or null when this Mac did. */
	readonly assistant: string | null;
	readonly pages: number | null;
	/** What the reading left out, in one sentence each. */
	readonly notes: readonly string[];
};

type ResumeStepProps = {
	readonly draft: ResumeDraft;
	readonly source: ResumeSource | null;
	/** The assistant connected in step one, or null. Only then is the paid reading offered. */
	readonly lane: string | null;
	readonly onRead: (draft: ResumeDraft, source: ResumeSource, file: File | null) => void;
	readonly onChange: (draft: ResumeDraft) => void;
	readonly onReading: (reading: boolean) => void;
};

type Reading =
	| { readonly kind: "idle" }
	| { readonly kind: "reading" }
	| { readonly kind: "refused"; readonly error: OnboardingRequestError; readonly file: File | null };

function asRefusal(error: Error): OnboardingRequestError {
	return error instanceof OnboardingRequestError ? error : new OnboardingRequestError(error.message, null, null, null);
}

function isPdf(file: File): boolean {
	return file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
}

function readNotes(truncated: boolean, dropped: number): readonly string[] {
	const notes: string[] = [];
	if (truncated) notes.push("The résumé was longer than Venator reads, so the end of it wasn’t read.");
	if (dropped === 1) notes.push("One value was left out because the pages don’t say it.");
	if (dropped > 1) notes.push(`${String(dropped)} values were left out because the pages don’t say them.`);
	return notes;
}

/** A group's heading with its Looks right check. Never ticked until the person ticks it. */
function GroupHeader({ title, checked, onCheck }: {
	readonly title: string;
	readonly checked: boolean;
	readonly onCheck: (checked: boolean) => void;
}) {
	return (
		<h2 className="group-header">
			{title}
			<label className="form-check">
				<input type="checkbox" checked={checked} onChange={(event) => onCheck(event.target.checked)} aria-label={`${title}: ${RESUME.looksRight}`} />
				{RESUME.looksRight}
			</label>
		</h2>
	);
}

/** The Contact group's order on the board: where to reach the person before how to link them. */
const CONTACT_ORDER: readonly ExtractedField[] = ["name", "location", "email", "phone", "linkedin"];

type TextFields = Readonly<Record<string, string>>;
type EntryField = { readonly key: string; readonly lines?: boolean };

const EXPERIENCE_FIELDS: readonly EntryField[] = [
	{ key: "role" }, { key: "org" }, { key: "location" }, { key: "dates" }, { key: "bullets", lines: true },
];
const EDUCATION_FIELDS: readonly EntryField[] = [
	{ key: "degree" }, { key: "org" }, { key: "location" }, { key: "date" }, { key: "gpa" }, { key: "coursework", lines: true },
];
const PROFICIENCY_FIELDS: readonly EntryField[] = [{ key: "label" }, { key: "items" }];

/** One entry's fields in a sheet: what the row stands for, editable. */
function EntryFields<Entry extends TextFields>({ section, entry, fields, onEntry }: {
	readonly section: SectionKey;
	readonly entry: Entry;
	readonly fields: readonly EntryField[];
	readonly onEntry: (entry: Entry) => void;
}) {
	return (
		<div className="sheet-fields">
			{fields.map((field) => (
				<label key={field.key}>
					{resumeFieldLabel(section, field.key)}
					{field.lines === true ? (
						<textarea className="text-field" value={entry[field.key] ?? ""} onChange={(event) => onEntry({ ...entry, [field.key]: event.target.value })} />
					) : (
						<input className="text-field" value={entry[field.key] ?? ""} onChange={(event) => onEntry({ ...entry, [field.key]: event.target.value })} />
					)}
				</label>
			))}
		</div>
	);
}

type Editing =
	| { readonly kind: "experience"; readonly index: number }
	| { readonly kind: "education"; readonly index: number }
	| { readonly kind: "skills" }
	| null;

function joined(...parts: readonly string[]): string {
	return parts.map((part) => part.trim()).filter((part) => part !== "").join(" · ");
}

function skillsOf(entries: readonly ProficiencyEntry[]): readonly string[] {
	return entries.flatMap((entry) => entry.items.split(",").map((item) => item.trim()).filter((item) => item !== ""));
}

/** A row's title and the line under it: the role or degree first, the place and dates after. */
function rowWords(headline: string, org: string, when: string) {
	const title = headline.trim() === "" ? org : headline;
	return { title, subtitle: joined(title === org ? "" : org, when) };
}

function Row({ title, subtitle, onOpen }: { readonly title: string; readonly subtitle: string; readonly onOpen: () => void }) {
	return (
		<button type="button" className="setup-row" onClick={onOpen}>
			<span className="setup-row-lines">
				<span className="setup-row-title">{title}</span>
				{subtitle === "" ? null : <span className="setup-row-subtitle">{subtitle}</span>}
			</span>
			<ChevronRight aria-hidden="true" className="chevron" />
		</button>
	);
}

/**
 * Step two: add a résumé, read it, check what was read.
 *
 * **Choosing a file is the press that reads it**, with the reader chosen beside the drop zone
 * and its cost said under it before anything is chosen. The whole-résumé reading is one request
 * on the connected assistant and is offered only when step one connected one; this Mac's
 * reading is free and is always offered. Neither retries into the other.
 *
 * **Nothing is confirmed for the person.** What was read lands in editable fields, grouped, and
 * each group has its own Looks right check, never pre-ticked. A section nobody ticks is left
 * out of the Profile (`draft.ts: resumeMapping`); the five contact facts gate Continue, because
 * a résumé in the Profile needs all five.
 */
export function ResumeStep({ draft, source, lane, onRead, onChange, onReading }: ResumeStepProps) {
	const [mode, setMode] = useState<ResumeParseMode>(lane === null ? "text" : "model");
	const [reading, setReading] = useState<Reading>({ kind: "idle" });
	const [over, setOver] = useState(false);
	const [pasteOpen, setPasteOpen] = useState(false);
	const [pasted, setPasted] = useState("");
	const [editing, setEditing] = useState<Editing>(null);
	const picker = useRef<HTMLInputElement | null>(null);
	/** Closes the gap between a press and the render that disables the next one. */
	const busy = useRef(false);
	/** A reading can take a minute: fold it into the draft as it stands when it answers. */
	const current = useRef(draft);
	current.current = draft;
	const assistant = lane === null ? null : assistantLabel(lane);

	const readFile = useCallback(
		(file: File, parse: ResumeParseMode) => {
			if (busy.current) return;
			if (!isPdf(file)) {
				setReading({ kind: "refused", error: new OnboardingRequestError(RESUME.notOnePdf, null, null, null), file: null });
				return;
			}
			busy.current = true;
			onReading(true);
			setReading({ kind: "reading" });
			importResumePdf(file, { parse, lane: parse === "model" ? lane : null })
				.then((response) => {
					setReading({ kind: "idle" });
					if (response.kind === "parsed") {
						const read = { name: file.name, assistant, pages: response.pages, notes: readNotes(response.truncated, response.dropped) };
						onRead(withParsedResume(current.current, response), read, file);
						return;
					}
					onRead(withSuggestion(current.current, response), { name: file.name, assistant: null, pages: null, notes: [] }, file);
				})
				.catch((error: Error) => setReading({ kind: "refused", error: asRefusal(error), file }))
				.finally(() => {
					busy.current = false;
					onReading(false);
				});
		},
		[assistant, lane, onRead, onReading],
	);

	const readPasted = useCallback(() => {
		if (busy.current || pasted.trim() === "") return;
		busy.current = true;
		onReading(true);
		setPasteOpen(false);
		setReading({ kind: "reading" });
		importResumeText(pasted)
			.then((suggestion) => {
				setReading({ kind: "idle" });
				onRead(withSuggestion(current.current, suggestion), { name: RESUME.pastedName, assistant: null, pages: null, notes: [] }, null);
			})
			.catch((error: Error) => setReading({ kind: "refused", error: asRefusal(error), file: null }))
			.finally(() => {
				busy.current = false;
				onReading(false);
			});
	}, [onRead, onReading, pasted]);

	const onDrop = useCallback(
		(event: DragEvent<HTMLDivElement>) => {
			event.preventDefault();
			setOver(false);
			const file = event.dataTransfer.files[0];
			if (file !== undefined) readFile(file, mode);
		},
		[mode, readFile],
	);

	const sections = draft.sections;
	const setValue = (field: ExtractedField, value: string) => onChange({ ...draft, values: { ...draft.values, [field]: value } });
	const acknowledgeAll = (checked: boolean) =>
		onChange({ ...draft, acknowledged: { name: checked, location: checked, phone: checked, email: checked, linkedin: checked } });
	const confirm = (keys: readonly SectionKey[], checked: boolean) =>
		onChange({ ...draft, sections: { ...sections, confirmed: { ...sections.confirmed, ...Object.fromEntries(keys.map((key) => [key, checked])) } } });
	const historyKeys: readonly SectionKey[] = (["experience", "education"] as const).filter((key) => sections[key].length > 0);
	const historyChecked = historyKeys.length > 0 && historyKeys.every((key) => sections.confirmed[key]);

	const setExperience = (index: number, entry: ExperienceEntry) =>
		onChange({ ...draft, sections: { ...sections, experience: sections.experience.map((old, at) => (at === index ? entry : old)) } });
	const setEducation = (index: number, entry: EducationEntry) =>
		onChange({ ...draft, sections: { ...sections, education: sections.education.map((old, at) => (at === index ? entry : old)) } });
	const setProficiency = (index: number, entry: ProficiencyEntry) =>
		onChange({
			...draft,
			sections: { ...sections, technical_proficiencies: sections.technical_proficiencies.map((old, at) => (at === index ? entry : old)) },
		});

	const chooser = (
		<input
			ref={picker}
			type="file"
			accept="application/pdf,.pdf"
			hidden
			onChange={(event) => {
				const file = event.target.files?.[0];
				event.target.value = "";
				if (file !== undefined) readFile(file, mode);
			}}
		/>
	);

	let editor: ReactNode = null;
	if (editing?.kind === "experience") {
		const entry = sections.experience[editing.index];
		if (entry !== undefined) {
			editor = <EntryFields section="experience" entry={entry} fields={EXPERIENCE_FIELDS} onEntry={(next) => setExperience(editing.index, next)} />;
		}
	} else if (editing?.kind === "education") {
		const entry = sections.education[editing.index];
		if (entry !== undefined) {
			editor = <EntryFields section="education" entry={entry} fields={EDUCATION_FIELDS} onEntry={(next) => setEducation(editing.index, next)} />;
		}
	} else if (editing?.kind === "skills") {
		editor = sections.technical_proficiencies.map((entry, index) => (
			<EntryFields key={String(index)} section="technical_proficiencies" entry={entry} fields={PROFICIENCY_FIELDS} onEntry={(next) => setProficiency(index, next)} />
		));
	}

	const refused = reading.kind === "refused" ? reading : null;
	const refusal =
		refused === null ? null : (
			<div className="setup-alert" role="alert">
				<p>{refused.error.message}</p>
				{refused.error.remedy === null ? null : <p>{refused.error.remedy}</p>}
				{refused.error.code === "runtime_unavailable" && refused.file !== null ? (
					<div>
						<button
							type="button"
							className="button"
							data-size="large"
							onClick={() => {
								setMode("text");
								if (refused.file !== null) readFile(refused.file, "text");
							}}
						>
							{RESUME.readThisMacInstead}
						</button>
					</div>
				) : null}
			</div>
		);

	const pasteSheet = (
		<Sheet
			open={pasteOpen}
			onOpenChange={setPasteOpen}
			title={RESUME.pasteTitle}
			description={RESUME.pasteDescription}
			actions={
				<>
					<SheetClose className="button" data-size="large">
						{RESUME.cancel}
					</SheetClose>
					<button type="button" className="button" data-size="large" data-tone="prominent" disabled={pasted.trim() === ""} onClick={readPasted}>
						{RESUME.pasteRead}
					</button>
				</>
			}
		>
			<textarea className="text-field" aria-label={RESUME.pasteTitle} value={pasted} onChange={(event) => setPasted(event.target.value)} />
		</Sheet>
	);

	if (source === null || reading.kind === "reading") {
		return (
			<>
				{chooser}
				{pasteSheet}
				<div
					className="drop-zone"
					data-over={over}
					onDragOver={(event) => {
						event.preventDefault();
						setOver(true);
					}}
					onDragLeave={() => setOver(false)}
					onDrop={onDrop}
				>
					{reading.kind === "reading" ? (
						<>
							<LoaderCircle aria-hidden="true" className="glyph-large spinner" />
							<p className="drop-title" role="status">
								{RESUME.reading}
							</p>
						</>
					) : (
						<>
							<FileGlyph aria-hidden="true" className="glyph-large" />
							<p className="drop-title">{RESUME.drop}</p>
							<div className="drop-actions">
								<button type="button" className="button" data-size="large" onClick={() => picker.current?.click()}>
									{RESUME.chooseFile}
								</button>
								<button type="button" className="button" data-size="large" onClick={() => setPasteOpen(true)}>
									{RESUME.pasteText}
								</button>
							</div>
						</>
					)}
				</div>
				{refusal}
				<section className="setup-section">
					<div className="setup-inline">
						<span>{RESUME.readWith}</span>
						<Popup label={RESUME.readWith} value={mode} onChange={(value) => setMode(value === "model" && lane !== null ? "model" : "text")}>
							{assistant === null ? null : <option value="model">{wholeResumeOption(assistant)}</option>}
							<option value="text">{RESUME.thisMac}</option>
						</Popup>
					</div>
					<p className="setup-caption">{mode === "model" && assistant !== null ? wholeResumeCaption(assistant) : THIS_MAC_CAPTION}</p>
				</section>
			</>
		);
	}

	return (
		<>
			{chooser}
			{pasteSheet}
			<div className="file-row">
				<FileText aria-hidden="true" className="icon" />
				<span className="setup-row-lines">
					<span className="setup-row-title">{source.name}</span>
					<span className="setup-row-subtitle">{readByLine(source.assistant, source.pages)}</span>
				</span>
				<button type="button" className="button" data-size="large" onClick={() => picker.current?.click()}>
					{RESUME.replace}
				</button>
			</div>
			{source.notes.length === 0 ? null : <p className="setup-caption">{source.notes.join(" ")}</p>}
			{refusal}

			<section className="setup-section">
				<GroupHeader title={RESUME.contact} checked={unconfirmedFields(draft).length === 0} onCheck={acknowledgeAll} />
				<div className="setup-group">
					{CONTACT_ORDER.map((field) => (
						<label key={field} className="setup-row">
							<span className="setup-row-label">{extractedFieldLabel(field)}</span>
							<input className="text-field" value={draft.values[field]} onChange={(event) => setValue(field, event.target.value)} />
						</label>
					))}
				</div>
			</section>

			{historyKeys.length === 0 ? null : (
				<section className="setup-section">
					<GroupHeader title={RESUME.history} checked={historyChecked} onCheck={(checked) => confirm(historyKeys, checked)} />
					<div className="setup-group">
						{sections.experience.map((entry, index) => (
							<Row key={`experience-${String(index)}`} {...rowWords(entry.role, entry.org, entry.dates)} onOpen={() => setEditing({ kind: "experience", index })} />
						))}
						{sections.education.map((entry, index) => (
							<Row key={`education-${String(index)}`} {...rowWords(entry.degree, entry.org, entry.date)} onOpen={() => setEditing({ kind: "education", index })} />
						))}
					</div>
				</section>
			)}

			{sections.technical_proficiencies.length === 0 ? null : (
				<section className="setup-section">
					<GroupHeader
						title={RESUME.skills}
						checked={sections.confirmed.technical_proficiencies}
						onCheck={(checked) => confirm(["technical_proficiencies"], checked)}
					/>
					<button type="button" className="chip-list setup-group" aria-label={RESUME.editSkills} onClick={() => setEditing({ kind: "skills" })}>
						{skillsOf(sections.technical_proficiencies).map((skill, index) => (
							<span key={`${skill}-${String(index)}`} className="chip-token">
								{skill}
							</span>
						))}
					</button>
				</section>
			)}

			<Sheet
				open={editor !== null}
				onOpenChange={(open) => {
					if (!open) setEditing(null);
				}}
				title={editing?.kind === "skills" ? RESUME.skills : RESUME.history}
			>
				{editor}
			</Sheet>
		</>
	);
}
