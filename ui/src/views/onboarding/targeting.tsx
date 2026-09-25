import { BAND_OPTIONS, lines, REMOTE_OPTIONS, type RemotePreference, type TargetingDraft } from "../../onboarding/draft.ts";
import { TARGETING } from "./copy.ts";
import { EmployerPicker } from "./employers.tsx";
import { Popup, TokenField } from "./parts.tsx";

type TargetingStepProps = {
	readonly draft: TargetingDraft;
	readonly onChange: (draft: TargetingDraft) => void;
};

function remoteOf(value: string): RemotePreference {
	return REMOTE_OPTIONS.find((option) => option.value === value)?.value ?? "acceptable";
}

/**
 * Step three: what to look for, and whose job boards to check.
 *
 * Titles and places are tokens over the draft's one-per-line text, the level is one contiguous
 * slice of the ladder, and the employers are the picker's. Nothing here writes: the flow's
 * Find Jobs writes the whole draft at once.
 */
export function TargetingStep({ draft, onChange }: TargetingStepProps) {
	const low = Math.min(draft.bandFrom, draft.bandTo);
	const high = Math.max(draft.bandFrom, draft.bandTo);
	return (
		<>
			<section className="setup-section" aria-label={TARGETING.jobs}>
				<h2 className="group-header">{TARGETING.jobs}</h2>
				<div className="setup-group">
					<div className="setup-row">
						<span className="setup-row-label">{TARGETING.titles}</span>
						<TokenField
							label={TARGETING.titles}
							tokens={lines(draft.roles)}
							placeholder={TARGETING.addTitle}
							onChange={(tokens) => onChange({ ...draft, roles: tokens.join("\n") })}
						/>
					</div>
					<div className="setup-row">
						<span className="setup-row-label">{TARGETING.level}</span>
						<Popup
							label={TARGETING.level}
							value={`${String(low)}-${String(high)}`}
							onChange={(value) => {
								const band = BAND_OPTIONS.find((option) => `${String(option.from)}-${String(option.to)}` === value);
								if (band !== undefined) onChange({ ...draft, bandFrom: band.from, bandTo: band.to });
							}}
						>
							{BAND_OPTIONS.map((option) => (
								<option key={`${String(option.from)}-${String(option.to)}`} value={`${String(option.from)}-${String(option.to)}`}>
									{option.label}
								</option>
							))}
						</Popup>
					</div>
					<div className="setup-row">
						<span className="setup-row-label">{TARGETING.places}</span>
						<TokenField
							label={TARGETING.places}
							tokens={lines(draft.locations)}
							placeholder={TARGETING.addPlace}
							onChange={(tokens) => onChange({ ...draft, locations: tokens.join("\n") })}
						/>
					</div>
					<div className="setup-row">
						<span className="setup-row-label">{TARGETING.remote}</span>
						<Popup label={TARGETING.remote} value={draft.remote} onChange={(value) => onChange({ ...draft, remote: remoteOf(value) })}>
							{REMOTE_OPTIONS.map((option) => (
								<option key={option.value} value={option.value}>
									{option.label}
								</option>
							))}
						</Popup>
					</div>
				</div>
			</section>
			<EmployerPicker boards={draft.boards} onChange={(boards) => onChange({ ...draft, boards })} />
		</>
	);
}
