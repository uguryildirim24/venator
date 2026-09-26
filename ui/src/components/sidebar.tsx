import { Ban, Binoculars, Bookmark, Clock3, Building2, CircleUser, Compass, Crosshair, EyeOff, PanelLeft, Send, type LucideIcon } from "lucide-react";

import type { DatabaseInfo, Funnel } from "../../shared/contracts.ts";
import { earlyReviewLabel, homeListLabel, SAMPLE_DATA_DETAIL, SAMPLE_DATA_STAMP } from "../labels.ts";
import { employersHash, profileHash, queueHash, triageHash, type HomeList, type Route } from "../router.ts";
import { RunStatus } from "../runs/status.tsx";

type Row = {
	readonly id: string;
	readonly label: string;
	readonly icon: LucideIcon;
	readonly hash: string;
	readonly count: number | null;
};

function listRow(list: HomeList, icon: LucideIcon, count: number | null): Row {
	return { id: list, label: homeListLabel(list), icon, hash: queueHash(list), count };
}

/** Which sidebar row the route belongs to. A Posting belongs to the list it was opened from. */
function activeRow(route: Route): string | null {
	if (route.name === "queue") return route.list;
	if (route.name === "triage") return "triage";
	if (route.name === "employers") return "employers";
	if (route.name === "profile") return "profile";
	if (route.name === "posting") {
		if (route.returnTo?.startsWith("#/inspector") === true) return null;
		return route.from;
	}
	return null;
}

type SidebarProps = {
	readonly route: Route;
	readonly funnel: Funnel | null;
	readonly database: DatabaseInfo | null;
	/** How many boards the Install checks: one source-health row each. */
	readonly boards: number;
	/** Whether a run can be offered: an Install with a Profile. */
	readonly configured: boolean;
	/** Nothing discovered yet: no Jev diagnostics to show and nothing for Jev it to run on. */
	readonly firstRun: boolean;
	/** The job list is being rebuilt, and the footer says so instead of the last fetch. */
	readonly updating: boolean;
	readonly onToggle: () => void;
	readonly onReload: () => void;
};

/**
 * The source list: three sections of the kit's Medium rows, and the last fetch at the foot.
 *
 * The selected row is a neutral pill; the symbols are ember. Counts are the funnel's, so they
 * agree with every list they name. Applications, Saved and Dismissed are disjoint — the
 * application states `approved` and `rejected` are their own rows — so the three add up to
 * the Postings with application progress.
 */
export function Sidebar({ route, funnel, database, boards, configured, firstRun, updating, onToggle, onReload }: SidebarProps) {
	const active = activeRow(route);
	const sections: readonly { readonly heading: string | null; readonly rows: readonly Row[] }[] = [
		{
			heading: null,
			rows: [
				listRow("queued", Crosshair, funnel?.queued ?? null),
				listRow("needs-review", Compass, funnel?.needsReview ?? null),
				listRow("unscored", Clock3, funnel?.unscored ?? null),
				...(firstRun ? [] : [{ id: "triage", label: earlyReviewLabel(), icon: Binoculars, hash: triageHash(), count: null }]),
			],
		},
		{
			heading: "Record",
			rows: [
				listRow("applied", Send, funnel === null ? null : funnel.applied - funnel.saved - funnel.dismissed),
				listRow("saved", Bookmark, funnel?.saved ?? null),
				listRow("dismissed", EyeOff, funnel?.dismissed ?? null),
				listRow("filtered", Ban, funnel?.hardKilled ?? null),
			],
		},
		{
			heading: "Sources",
			rows: [
				{ id: "employers", label: "Employers", icon: Building2, hash: employersHash(), count: boards },
				{ id: "profile", label: "Profile", icon: CircleUser, hash: profileHash(), count: null },
			],
		},
	];

	return (
		<aside className="sidebar" aria-label="Lists">
			<div className="sidebar-top" data-tauri-drag-region>
				<button type="button" className="icon-button" onClick={onToggle} aria-label="Hide the sidebar" title="Hide the sidebar">
					<PanelLeft aria-hidden="true" className="icon" />
				</button>
			</div>
			<nav className="sidebar-nav">
				{sections.map((section) => (
					<div key={section.heading ?? "lists"}>
						{section.heading === null ? null : <h2 className="sidebar-heading">{section.heading}</h2>}
						{section.rows.map((row) => (
							<a key={row.id} className="sidebar-row" href={row.hash} aria-current={row.id === active ? "page" : undefined}>
								<row.icon aria-hidden="true" className="icon" />
								<span>{row.label}</span>
								{row.count === null || row.count === 0 ? null : <span className="sidebar-count">{row.count.toLocaleString("en-US")}</span>}
							</a>
						))}
					</div>
				))}
			</nav>
			{database?.kind === "fixture" ? (
				<p className="sidebar-stamp" title={SAMPLE_DATA_DETAIL}>
					{SAMPLE_DATA_STAMP}
				</p>
			) : null}
			{configured ? (
				<RunStatus
					boards={boards}
					lastFetchAt={database?.lastFetchAt ?? null}
					jevPause={database?.jevPause ?? null}
					jevIt={!firstRun}
					updating={updating}
					onSettled={onReload}
				/>
			) : null}
		</aside>
	);
}
