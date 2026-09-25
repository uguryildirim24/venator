import { ChevronRight } from "lucide-react";

import type { Funnel } from "../../shared/contracts.ts";
import { earlyReviewLabel, homeListLabel } from "../labels.ts";
import { queueHash, type HomeList } from "../router.ts";

/** Why a list other than For you is empty, in its own terms. */
const REASONS = {
	queued: "No current Jev result says to look first. Postings awaiting Jev are still visible in Awaiting Jev.",
	"needs-review": "No current Jev result asks for review right now.",
	unscored: "Every Posting has a current Jev result or is already excluded by a Hard Filter.",
	applied: "No application is in progress. Saved and dismissed Postings have their own lists.",
	saved: "Nothing is saved. Save a Posting from its toolbar and it waits here.",
	dismissed: "Nothing is dismissed. A dismissed Posting keeps its place here and can be restored.",
	filtered: "No Posting was excluded by a Hard Filter.",
	triage: "No unavailable or out-of-date Jev results to diagnose.",
	inspector: "No Posting matches these filters. Every Posting keeps its decision, so widening them finds it.",
} satisfies Record<HomeList | "triage" | "inspector", string>;

type Destination = { readonly label: string; readonly hash: string; readonly count: number; readonly what: string };

/**
 * Where the Postings are when For you is empty.
 *
 * Every Posting is somewhere, with the Filter Decision and the reason that put it there. An
 * empty For you that said so and stopped would read as "the run found nothing", which is the
 * opposite of what happened. So each list that holds Postings is named, counted, and linked.
 */
function destinations(funnel: Funnel): readonly Destination[] {
	return [
		{ label: homeListLabel("needs-review"), hash: queueHash("needs-review"), count: funnel.needsReview, what: "need a closer look before preparing" },
		{ label: homeListLabel("filtered"), hash: queueHash("filtered"), count: funnel.hardKilled, what: "were excluded by a Hard Filter or Jev" },
		{ label: homeListLabel("applied"), hash: queueHash("applied"), count: funnel.applied - funnel.saved - funnel.dismissed, what: "have an application in progress" },
		{ label: homeListLabel("saved"), hash: queueHash("saved"), count: funnel.saved, what: "are saved" },
		{ label: homeListLabel("unscored"), hash: queueHash("unscored"), count: funnel.unscored, what: "await a current Jev result" },
	].filter((entry) => entry.count > 0);
}

type EmptyListProps = {
	readonly source: HomeList | "triage" | "inspector";
	readonly search: string;
	/** The list has Postings, just not on this page. */
	readonly beyond: boolean;
	readonly funnel: Funnel | null;
	readonly onReload: () => void;
};

export function EmptyList({ source, search, beyond, funnel, onReload }: EmptyListProps) {
	if (beyond) {
		return (
			<div className="empty">
				<p className="empty-title">No Postings on this page</p>
				<p>The list is shorter than this page. Go back a page to see it.</p>
			</div>
		);
	}
	if (search !== "") {
		return (
			<div className="empty">
				<p className="empty-title">Nothing matches “{search}”</p>
				<p>The search looks at titles, employers and locations in this list only.</p>
			</div>
		);
	}
	const elsewhere = source === "queued" && funnel !== null ? destinations(funnel) : [];
	const title = source === "triage" ? `${earlyReviewLabel()} is empty` : source === "inspector" ? "No Posting matches" : `${homeListLabel(source)} is empty`;
	return (
		<div className="empty">
			<p className="empty-title">{title}</p>
			<p>{REASONS[source]}</p>
			{elsewhere.length === 0 ? null : (
				<nav className="empty-destinations" aria-label="Where the Postings are">
					{elsewhere.map((entry) => (
						<a key={entry.label} href={entry.hash}>
							<span>
								{entry.label}
								<span className="form-help">
									{entry.count.toLocaleString("en-US")} {entry.count === 1 ? "Posting" : "Postings"} {entry.what}
								</span>
							</span>
							<ChevronRight aria-hidden="true" className="glyph" />
						</a>
					))}
				</nav>
			)}
			<div className="empty-actions">
				<button type="button" className="button" onClick={onReload}>
					Reload
				</button>
			</div>
		</div>
	);
}
