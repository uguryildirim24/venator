import { ChevronsUpDown } from "lucide-react";
import { useCallback, useState, type KeyboardEvent, type ReactNode } from "react";

import { ExternalLink } from "../../components/external-link.tsx";
import { OnboardingRequestError, storeJevKey } from "../../onboarding/api.ts";
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
