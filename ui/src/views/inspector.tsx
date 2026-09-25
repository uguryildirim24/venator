import { X } from "lucide-react";
import { useCallback, useMemo, type RefObject } from "react";

import { POSTING_STATUSES, type Funnel, type PostingEntry, type PostingStatus } from "../../shared/contracts.ts";
import { usePostings } from "../api.ts";
import { Toolbar } from "../components/toolbar.tsx";
import { formatDay, formatMoment } from "../format.ts";
import { useListNavigation } from "../keys.ts";
import { originLabel, postingCountLabel, ruleLabel, statusLabel } from "../labels.ts";
import { homeListOf, inspectorQuery } from "../lists.ts";
import { inspectorHash, navigate, PAGE_SIZE, postingHash, replaceRoute, type InspectorFilters } from "../router.ts";

const NO_ENTRIES: readonly PostingEntry[] = [];

function statusCount(funnel: Funnel, status: PostingStatus): number {
	if (status === "unscored") return funnel.unscored;
	if (status === "hard-killed") return funnel.hardKilled;
	if (status === "queued") return funnel.queued;
	if (status === "needs-review") return funnel.needsReview;
	if (status === "closed") return funnel.closed;
	if (status === "no-text") return funnel.noText;
	if (status === "too-long") return funnel.tooLong;
	if (status === "protected") return funnel.protected;
	if (status === "not-filtered") return funnel.notFiltered;
	return funnel.applied;
}

type InspectorProps = {
	readonly filters: InspectorFilters;
	readonly page: number;
	readonly funnel: Funnel | null;
	readonly reloadToken: number;
	readonly searchField: RefObject<HTMLInputElement | null>;
	readonly sidebarHidden: boolean;
	readonly onShowSidebar: () => void;
};

/**
 * Every Posting the pipeline has seen, with the Filter Decision that placed it: the one screen
 * that is data for scanning, so the one place for the kit's table. It answers "what did this
 * rule exclude" in two presses — the rule, then the row — and a row opens its Posting with the
 * reason beside it.
 */
export function InspectorView({ filters, page, funnel, reloadToken, searchField, sidebarHidden, onShowSidebar }: InspectorProps) {
	const returnTo = inspectorHash(filters, page);
	const query = useMemo(() => inspectorQuery(filters, page * PAGE_SIZE), [filters, page]);
	const state = usePostings(query, reloadToken);
	const entries = state.status === "ready" ? state.value.entries : NO_ENTRIES;
	const total = state.status === "ready" ? state.value.total : null;

	const open = useCallback(
		(index: number) => {
			const entry = entries[index];
			if (entry !== undefined) navigate(postingHash(entry.posting.key, homeListOf(entry), returnTo));
		},
		[entries, returnTo],
	);
	const [selected, setSelected] = useListNavigation(entries.length, open);
	const setFilters = (next: InspectorFilters) => navigate(inspectorHash(next));
	const commitSearch = useCallback((search: string) => replaceRoute(inspectorHash({ ...filters, search })), [filters]);

	const last = page * PAGE_SIZE + entries.length;

	return (
		<>
			<Toolbar
				title="Filter inspector"
				subtitle={total === null ? null : postingCountLabel(total, 0)}
				search={{ value: filters.search, placeholder: "Search", onCommit: commitSearch, field: searchField }}
				sidebarHidden={sidebarHidden}
				onShowSidebar={onShowSidebar}
				layout="single"
			/>
			<section className="table-view" aria-label="Filter inspector">
				<div className="table-filters">
					<div className="segmented" role="group" aria-label="Where a Posting stands">
						<button type="button" aria-pressed={filters.status === null} onClick={() => setFilters({ ...filters, status: null })}>
							All
						</button>
						{POSTING_STATUSES.map((status) => (
							<button key={status} type="button" aria-pressed={filters.status === status} onClick={() => setFilters({ ...filters, status })}>
								{statusLabel(status)}
								{funnel === null ? null : <span className="tabular"> {statusCount(funnel, status).toLocaleString("en-US")}</span>}
							</button>
						))}
					</div>
					{filters.rule === null ? null : (
						<button type="button" className="chip" onClick={() => setFilters({ ...filters, rule: null })} aria-label={`Stop filtering by ${ruleLabel(filters.rule)}`}>
							{ruleLabel(filters.rule)}
							<X aria-hidden="true" className="glyph" />
						</button>
					)}
					{total === null || total <= PAGE_SIZE ? null : (
						<span className="table-pages">
							<span className="tabular">
								{(page * PAGE_SIZE + 1).toLocaleString("en-US")}–{last.toLocaleString("en-US")} of {total.toLocaleString("en-US")}
							</span>
							<button type="button" className="button" disabled={page === 0} onClick={() => navigate(inspectorHash(filters, page - 1))}>
								Previous
							</button>
							<button type="button" className="button" disabled={last >= total} onClick={() => navigate(inspectorHash(filters, page + 1))}>
								Next
							</button>
						</span>
					)}
				</div>
				<div className="table-scroll">
					{state.status === "error" ? (
						<div className="empty">
							<p className="empty-title">The inspector could not read the view database</p>
							<p>{state.message}</p>
						</div>
					) : null}
					{state.status === "ready" && entries.length === 0 ? (
						<div className="empty">
							<p className="empty-title">No Posting matches these filters</p>
							<p>Every Posting keeps its decision and its reason, so widening the filters finds the one you are after.</p>
						</div>
					) : null}
					{entries.length === 0 ? null : (
						<table className="table" aria-label="Postings and their Filter Decisions">
							<thead>
								<tr>
									<th scope="col">Status</th>
									<th scope="col">Rule</th>
									<th scope="col" className="table-fill">Title</th>
									<th scope="col">Employer</th>
									<th scope="col">Decided</th>
								</tr>
							</thead>
							<tbody>
								{entries.map((entry, index) => {
									const decision = entry.hardFilter;
									return (
										<tr
											key={entry.posting.key}
											aria-selected={index === selected}
											tabIndex={index === selected ? 0 : -1}
											onClick={() => {
												setSelected(index);
												open(index);
											}}
										>
											<td>{statusLabel(entry.status)}</td>
											<td className="secondary" title={decision?.reason ?? undefined}>
												{decision?.rule === null || decision?.rule === undefined ? "" : ruleLabel(decision.rule)}
											</td>
											<td className="table-fill">{entry.posting.title}</td>
											<td className="secondary">{originLabel(entry.posting)}</td>
											<td className="secondary tabular" title={decision === null ? undefined : formatMoment(decision.decidedAt)}>
												{decision === null ? "Not yet" : formatDay(decision.decidedAt)}
											</td>
										</tr>
									);
								})}
							</tbody>
						</table>
					)}
				</div>
			</section>
		</>
	);
}
