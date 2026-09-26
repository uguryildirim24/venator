import { ChevronRight, ChevronsUpDown } from "lucide-react";
import { useCallback, useState, type KeyboardEvent, type ReactNode } from "react";

import { ExternalLink } from "../../components/external-link.tsx";
import { resumeFieldLabel } from "../../labels.ts";
import { OnboardingRequestError, storeJevKey } from "../../onboarding/api.ts";
import type { ProficiencyEntry, SectionKey } from "../../onboarding/draft.ts";
import { CONNECT } from "./copy.ts";

/** A native menu drawn as the kit's popup button. */
export function Popup({ label, value, onChange, children }: {
	readonly label: string;
	readonly value: string;
	readonly onChange: (value: string) => void;
	readonly children: ReactNode;
}) {
	return (
		<span className="popup">
			<select aria-label={label} value={value} onChange={(event) => onChange(event.target.value)}>
				{children}
			</select>
			<ChevronsUpDown aria-hidden="true" className="icon" />
		</span>
	);
}

/**
 * Words as tokens: Return, a comma or leaving the field adds one; pressing a token or
 * Backspace in the empty field takes one away. The value is one entry per line, which is how
 * the draft already holds search terms and places.
 */
export function TokenField({ label, tokens, placeholder, onChange }: {
	readonly label: string;
	readonly tokens: readonly string[];
	readonly placeholder: string;
	readonly onChange: (tokens: readonly string[]) => void;
}) {
	const [typed, setTyped] = useState("");
	const commit = useCallback(() => {
		const word = typed.trim();
		setTyped("");
		if (word === "" || tokens.includes(word)) return;
		onChange([...tokens, word]);
	}, [onChange, tokens, typed]);
	const onKeyDown = useCallback(
		(event: KeyboardEvent<HTMLInputElement>) => {
			if (event.key === "Enter" || event.key === ",") {
				event.preventDefault();
				commit();
				return;
			}
			if (event.key === "Backspace" && typed === "" && tokens.length > 0) onChange(tokens.slice(0, -1));
		},
		[commit, onChange, tokens, typed],
	);
	return (
		<div className="token-field">
			{tokens.map((token) => (
				<button key={token} type="button" className="token" aria-label={`Remove ${token}`} title={`Remove ${token}`} onClick={() => onChange(tokens.filter((entry) => entry !== token))}>
					{token}
				</button>
			))}
			<input
				aria-label={label}
				value={typed}
				placeholder={placeholder}
				onChange={(event) => setTyped(event.target.value)}
				onKeyDown={onKeyDown}
				onBlur={commit}
			/>
		</div>
	);
}

/** A link that opens in the person's own browser, never in this window. */
export function OutsideLink({ href, children }: { readonly href: string; readonly children: string }) {
	return (
		<ExternalLink href={href} className="text-link" title={href}>
			{children}
		</ExternalLink>
	);
}

/**
 * The Jev key field: paste, Save Key, and it goes to this Mac's Keychain.
 *
 * The key is sent once, to `POST /api/onboarding/settings`, and never read back — the server
 * answers only whether one is present. The field is cleared the moment the save answers.
 */
export function JevKeyField({ present, onSaved }: {
	readonly present: boolean;
	readonly onSaved: () => void;
}) {
	const [key, setKey] = useState("");
	const [saving, setSaving] = useState(false);
	const [refusal, setRefusal] = useState<string | null>(null);
	const save = useCallback(() => {
		if (key.trim() === "" || saving) return;
		setSaving(true);
		setRefusal(null);
		storeJevKey(key)
			.then(() => {
				setKey("");
				onSaved();
			})
			.catch((error: Error) => {
				setRefusal(error instanceof OnboardingRequestError ? error.message : "The key could not be saved.");
			})
			.finally(() => setSaving(false));
	}, [key, onSaved, saving]);
	return (
		<>
			<div className="key-box">
				<input
					className="text-field"
					type="password"
					autoComplete="off"
					spellCheck={false}
					aria-label="TypeSafe key"
					placeholder={present ? CONNECT.keyReplacePlaceholder : CONNECT.keyPlaceholder}
					value={key}
					onChange={(event) => setKey(event.target.value)}
					onKeyDown={(event) => {
						if (event.key === "Enter") save();
					}}
				/>
				<button type="button" className="button" data-size="large" disabled={key.trim() === "" || saving} onClick={save}>
					{CONNECT.saveKey}
				</button>
			</div>
			{refusal === null ? null : <p className="setup-caption" role="alert">{refusal}</p>}
		</>
	);
}

type TextFields = Readonly<Record<string, string>>;
export type EntryField = { readonly key: string; readonly lines?: boolean };

/** The fields of one résumé entry, in `resume.yaml`'s own keys; the sheet shows them in this order. */
export const EXPERIENCE_FIELDS: readonly EntryField[] = [
	{ key: "role" }, { key: "org" }, { key: "location" }, { key: "dates" }, { key: "bullets", lines: true },
];
export const EDUCATION_FIELDS: readonly EntryField[] = [
	{ key: "degree" }, { key: "org" }, { key: "location" }, { key: "date" }, { key: "gpa" }, { key: "coursework", lines: true },
];
export const PROFICIENCY_FIELDS: readonly EntryField[] = [{ key: "label" }, { key: "items" }];

/** One entry's fields in a sheet: what the row stands for, editable. */
export function EntryFields<Entry extends TextFields>({ section, entry, fields, onEntry }: {
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

function joined(...parts: readonly string[]): string {
	return parts.map((part) => part.trim()).filter((part) => part !== "").join(" · ");
}

export function skillsOf(entries: readonly ProficiencyEntry[]): readonly string[] {
	return entries.flatMap((entry) => entry.items.split(",").map((item) => item.trim()).filter((item) => item !== ""));
}

/** A row's title and the line under it: the role or degree first, the place and dates after. */
export function rowWords(headline: string, org: string, when: string) {
	const title = headline.trim() === "" ? org : headline;
	return { title, subtitle: joined(title === org ? "" : org, when) };
}

/** A grouped row that opens a sheet: its words, and a chevron. */
export function Row({ title, subtitle, onOpen }: { readonly title: string; readonly subtitle: string; readonly onOpen: () => void }) {
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
