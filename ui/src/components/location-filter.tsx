import { ChevronRight, MapPin } from "lucide-react";
import { useState } from "react";

import { saveLocations, type LocationSelection, type RequestState } from "../api.ts";

type Props = { readonly locations: RequestState<LocationSelection>; readonly onReload: () => void };

/** A read-side, Install-wide choice. Nothing here changes Profile rules or Filter Decisions. */
export function LocationFilter({ locations, onReload }: Props) {
	const [error, setError] = useState<string | null>(null);
	const [busy, setBusy] = useState(false);
	const selected = locations.status === "ready" ? locations.value.selected : [];
	const choices = locations.status === "ready" ? locations.value.choices : [];
	const groups = choices.filter((choice) => !choice.parent);
	function option(choice: (typeof choices)[number]) {
		return <label key={choice.key} className="location-option">
			<input type="checkbox" checked={selected.includes(choice.key)} disabled={busy} onChange={() => change(selected.includes(choice.key) ? selected.filter((key) => key !== choice.key) : [...selected, choice.key])} />
			<span>{choice.label}</span><span className="tabular location-count">{choice.count}</span>
		</label>;
	}
	async function change(next: readonly string[]) {
		setBusy(true);
		setError(null);
		try { await saveLocations(next); onReload(); }
		catch { setError("The location choice could not be saved."); }
		finally { setBusy(false); }
	}
	return (
		<div className="location-filter">
			<details>
				<summary className="location-trigger"><MapPin className="icon" aria-hidden="true" /> {selected.length ? `Locations · ${selected.length} selected` : "All locations"}</summary>
				<div className="location-menu">
					<p>Show Postings in</p>
					{groups.map((group) => {
						const children = choices.filter((choice) => choice.parent === group.key);
						return children.length ? <details key={group.key} open={selected.includes(group.key) || children.some((child) => selected.includes(child.key)) || undefined}>
							<summary className="location-option location-group-summary"><ChevronRight className="icon location-group-chevron" aria-hidden="true" /><span>{group.label}</span><span className="tabular location-count">{group.count}</span></summary>
							<div className="location-children">
								{option({ ...group, label: `All ${group.label}` })}
								{children.map(option)}
							</div>
						</details> : option(group);
					})}
					{choices.length === 0 ? <p>No Posting locations yet.</p> : null}
					{selected.length ? <button type="button" className="button" disabled={busy} onClick={() => change([])}>Clear location filter</button> : null}
				</div>
			</details>
			{error ? <span role="alert">{error}</span> : null}
		</div>
	);
}
