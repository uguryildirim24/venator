import { ChevronLeft, ChevronRight } from "lucide-react";
import { useCallback, useMemo, useState, type RefObject } from "react";

import type { Funnel, JevTriageDecision, JevTriageEntry, PostingEntry } from "../../shared/contracts.ts";
import { useJevTriage, useLocations, usePostingDetail, usePostings } from "../api.ts";
import { useApplicationControl } from "../application.ts";
import { useDelayed } from "../delayed.ts";
import { CellList, type Cell, type CellGroup } from "../components/cells.tsx";
import { LocationFilter } from "../components/location-filter.tsx";
import { PostingPage } from "../components/posting-page.tsx";
import { PostingActions, Toolbar, type SearchProps } from "../components/toolbar.tsx";
import { dayKey, formatDay, formatDayHeader, formatMoment, formatTime } from "../format.ts";
import { useKeyBindings, type KeyBinding } from "../keys.ts";
import {
	applicationStateLabel,
	EARLY_REVIEW_CAVEAT,
	earlyReviewLabel,
	homeListLabel,
	jevListLabel,
	jevSkipLabel,
	jevStateLabel,
	originLabel,
	postingCountLabel,
	ruleLabel,
} from "../labels.ts";
import { homeListOf, homeListQuery, inspectorQuery, triageListQuery } from "../lists.ts";
import {
	inspectorHash,
	navigate,
	PAGE_SIZE,
	parseRoute,
	postingHash,
	queueHash,
	replaceRoute,
	triageHash,
	type FocusFrom,
	type HomeList,
	type InspectorFilters,
	type Route,
} from "../router.ts";
import { EmptyList } from "./empty-list.tsx";
import { AwaitingKey, UpdatingState, useJevKeyMissing } from "./first-run.tsx";
import { FIRST_RUN } from "./onboarding/copy.ts";

/** What the list column is showing: a home list, early review, or the inspector's results. */
type Source =
	| { readonly kind: "list"; readonly list: HomeList; readonly search: string; readonly page: number }
	| { readonly kind: "triage"; readonly search: string; readonly page: number; readonly decision: JevTriageDecision | null }
	| { readonly kind: "inspector"; readonly filters: InspectorFilters; readonly page: number };

function sourceOf(route: Route): Source {
	if (route.name === "triage") return { kind: "triage", decision: route.decision, search: route.search, page: route.page ?? 0 };
	if (route.name === "queue") return { kind: "list", list: route.list, search: route.search, page: route.page ?? 0 };
	if (route.name === "posting") {
		if (route.returnTo !== undefined) {
			const back = parseRoute(route.returnTo);
			if (back.name === "queue" || back.name === "triage" || back.name === "inspector") return sourceOf(back);
		}
		return route.from === "triage" ? sourceOf({ name: "triage", decision: null, search: "" }) : sourceOf({ name: "queue", list: route.from, search: "" });
	}
	return { kind: "list", list: "queued", search: "", page: 0 };
}

function sourceHash(source: Source, page = source.page): string {
	if (source.kind === "triage") return triageHash(source.decision, source.search, page);
	if (source.kind === "inspector") return inspectorHash(source.filters, page);
	return queueHash(source.list, source.search, page);
}

/* ---------------------------------------------------------------- cells */

function entryCell(entry: PostingEntry, list: HomeList | null): Cell {
	const { posting } = entry;
	const verified = entry.assessment?.lastVerifiedAt ?? null;
	const meta = [originLabel(posting), posting.location].filter((part) => part !== null && part !== "").join(" · ");
	const state = entry.application?.state ?? null;
	let status: Cell["status"] = null;
	if (entry.status === "hard-killed") {
		status = { text: entry.hardFilter?.verdict === "kill" && entry.hardFilter.rule ? ruleLabel(entry.hardFilter.rule) : entry.jev?.decision === "exclude" ? jevSkipLabel() : "Excluded", glyph: "excluded" };
	} else if (list === "applied" && state !== null) {
		status = {
			text: `${applicationStateLabel(state)}${entry.application?.since ? ` · ${formatDay(entry.application.since)}` : ""}`,
			glyph: state === "prepared" ? "ready" : null,
		};
	} else if (state === "prepared") {
		status = { text: "Documents ready", glyph: "ready" };
	} else if (entry.jev?.decision === "prioritize" || entry.jev?.decision === "review") {
		status = { text: jevListLabel(entry.jev.decision), glyph: "info" };
	}
	return {
		key: posting.key,
		title: posting.title,
		meta,
		time: verified === null ? formatDay(posting.discoveredAt) : formatTime(verified),
		stamp: verified === null ? `Found ${formatMoment(posting.discoveredAt)}` : `Verified ${formatMoment(verified)}`,
		isNew: entry.isNew,
		status,
	};
}

/**
 * Cells under day headers, from the timestamp the list is ordered by. Postings never verified
 * sort after every verified one, by when they were found, and sit under their own header so no
 * day header appears twice.
 */
function dayGroups(entries: readonly PostingEntry[], list: HomeList | null): CellGroup[] {
	const groups: CellGroup[] = [];
	for (const entry of entries) {
		const verified = entry.assessment?.lastVerifiedAt ?? null;
		const key = verified === null ? "unverified" : dayKey(verified);
		const last = groups.at(-1);
		const cell = entryCell(entry, list);
		if (last?.key === key) groups[groups.length - 1] = { ...last, cells: [...last.cells, cell] };
		else groups.push({ key, header: verified === null ? "Not verified yet" : formatDayHeader(verified), cells: [cell] });
	}
	return groups;
}

/** Diagnostics keep the API's verification order; each cell names its unavailable or stale state. */
function diagnosticGroups(entries: readonly JevTriageEntry[]): CellGroup[] {
	const cells: Cell[] = entries.map(({ posting, triage }) => ({
		key: posting.key,
		title: posting.title,
		meta: [originLabel(posting), posting.location].filter((part) => part !== null && part !== "").join(" · "),
		time: "",
		stamp: "",
		isNew: false,
		status: { text: jevStateLabel(triage.state), glyph: "info" },
	}));
	return cells.length === 0 ? [] : [{ key: "diagnostics", header: homeListLabel("unscored"), cells }];
}

/* ---------------------------------------------------------------- the view */

type WorkspaceProps = {
	readonly route: Route;
	readonly reloadToken: number;
	readonly onReload: () => void;
	readonly funnel: Funnel | null;
	readonly searchField: RefObject<HTMLInputElement | null>;
	readonly sidebarHidden: boolean;
	readonly onShowSidebar: () => void;
};

/**
 * The three-pane grammar every list shares: the list, and the selected Posting as a page.
 *
 * `#/queue` and `#/triage` show their first Posting; choosing another replaces the route with
 * that Posting's own address, so the selection is a link and Back leaves the list rather than
 * walking it.
 */
export function Workspace({ route, reloadToken, onReload, funnel, searchField, sidebarHidden, onShowSidebar }: WorkspaceProps) {
	const source = sourceOf(route);
	const locations = useLocations(reloadToken);
	const locationFiltered = locations.status === "ready" && locations.value.selected.length > 0;
	const offset = source.page * PAGE_SIZE;
	const postingsQuery =
		source.kind === "list"
			? homeListQuery(source.list, source.search, offset)
			: source.kind === "inspector"
				? inspectorQuery(source.filters, offset)
				: null;
	const postings = usePostings(postingsQuery, reloadToken);
	const triage = useJevTriage(source.kind === "triage" ? triageListQuery(source.decision, source.search, offset) : null, reloadToken);

	const listState = source.kind === "triage" ? triage : postings;
	const keys = useMemo(() => {
		if (source.kind === "triage") return triage.status === "ready" ? triage.value.entries.map((entry) => entry.posting.key) : [];
		return postings.status === "ready" ? postings.value.entries.map((entry) => entry.posting.key) : [];
	}, [source.kind, postings, triage]);
	const groups = useMemo(() => {
		if (source.kind === "triage") return triage.status === "ready" ? diagnosticGroups(triage.value.entries) : [];
		return postings.status === "ready" ? dayGroups(postings.value.entries, source.kind === "list" ? source.list : null) : [];
	}, [source, postings, triage]);

	/*
	 * Awaiting Jev with no key: the list stays, nothing is selected for the person, and the
	 * page's place says what the Postings are waiting for. Opening one still shows it.
	 */
	const [keyToken, setKeyToken] = useState(0);
	const awaitingList = source.kind === "list" && source.list === "unscored" && source.search === "";
	const keyMissing = useJevKeyMissing(awaitingList, reloadToken + keyToken);
	const awaitingKey = awaitingList && keyMissing && route.name !== "posting" && keys.length > 0;
	const selectedKey = route.name === "posting" && (!locationFiltered || listState.status !== "ready" || keys.includes(route.key)) ? route.key : awaitingKey ? null : (keys[0] ?? null);
	const detail = usePostingDetail(selectedKey, reloadToken);
	const control = useApplicationControl(selectedKey, reloadToken, onReload);
	const [keyboardMoves, setKeyboardMoves] = useState(0);

	const from: FocusFrom = source.kind === "triage" ? "triage" : source.kind === "list" ? source.list : "queued";
	const returnTo = sourceHash(source);
	const select = useCallback(
		(key: string) => {
			const entry = postings.status === "ready" ? postings.value.entries.find((candidate) => candidate.posting.key === key) : undefined;
			const listFrom = source.kind === "inspector" && entry !== undefined ? homeListOf(entry) : from;
			replaceRoute(postingHash(key, listFrom, returnTo));
		},
		[from, postings, returnTo, source.kind],
	);

	const move = useCallback(
		(to: (index: number) => number) => {
			if (keys.length === 0) return;
			const index = selectedKey === null ? -1 : keys.indexOf(selectedKey);
			const next = keys[Math.max(0, Math.min(keys.length - 1, to(index)))];
			if (next === undefined) return;
			select(next);
			setKeyboardMoves((count) => count + 1);
		},
		[keys, select, selectedKey],
	);
	const bindings = useMemo<readonly KeyBinding[]>(
		() => [
			{ keys: ["j", "ArrowDown"], label: "j", description: "next Posting", run: () => move((index) => index + 1) },
			{ keys: ["k", "ArrowUp"], label: "k", description: "previous Posting", run: () => move((index) => index - 1) },
			{ keys: ["g"], label: "g", description: "first Posting", run: () => move(() => 0) },
			{ keys: ["G"], label: "G", description: "last Posting", run: () => move(() => keys.length - 1) },
		],
		[keys.length, move],
	);
	useKeyBindings(bindings);

	const total = listState.status === "ready" ? listState.value.total : null;
	const fresh = source.kind !== "triage" && postings.status === "ready" ? postings.value.newCount : 0;
	const title = source.kind === "triage" ? earlyReviewLabel() : source.kind === "inspector" ? "Filter inspector" : homeListLabel(source.list);
	const search = source.kind === "inspector" ? source.filters.search : source.search;
	const updating = useDelayed(listState.status === "loading");
	const subtitle =
		listState.status === "loading"
			? FIRST_RUN.updatingToolbar
			: total === null
			? null
			: search !== ""
				? `${total.toLocaleString("en-US")} matching “${search}”`
				: postingCountLabel(total, source.kind === "triage" ? 0 : fresh);

	const commitSearch = useCallback(
		(next: string) => {
			if (source.kind === "triage") replaceRoute(triageHash(source.decision, next));
			else if (source.kind === "inspector") replaceRoute(inspectorHash({ ...source.filters, search: next }));
			else replaceRoute(queueHash(source.list, next));
		},
		[source],
	);
	const searchProps: SearchProps = { value: search, placeholder: "Search", onCommit: commitSearch, field: searchField };

	const shown = source.kind === "triage" ? (triage.status === "ready" ? triage.value.entries.length : 0) : postings.status === "ready" ? postings.value.entries.length : 0;
	const pages = total === null || total <= PAGE_SIZE ? null : { first: offset + 1, last: offset + shown, total };
	const current = detail.status === "ready" && detail.value.posting.key === selectedKey ? detail.value : null;

	return (
		<>
			<Toolbar
				title={title}
				subtitle={subtitle}
				search={searchProps}
				sidebarHidden={sidebarHidden}
				onShowSidebar={onShowSidebar}
				actions={current === null ? null : <PostingActions detail={current} control={control} />}
			/>
			{updating ? (
				<UpdatingState />
			) : (
			// Keyed by the list, so choosing another in the sidebar is a view arriving (window.css,
			// "A view arriving") while choosing a Posting within it is not.
			<div className="panes" key={source.kind === "list" ? source.list : source.kind}>
				<section className="list-pane" aria-label={title}>
					<LocationFilter locations={locations} onReload={() => { onReload(); if (source.page > 0) replaceRoute(sourceHash(source, 0)); }} />
					<div className="list-scroll">
						{listState.status === "error" ? (
							<div className="empty">
								<p className="empty-title">The list could not be read</p>
								<p>{listState.message}</p>
							</div>
						) : null}
						{listState.status === "ready" && shown === 0 ? (
							locationFiltered && search === "" && source.page === 0 && total === 0 ? <div className="empty"><p className="empty-title">No Postings in these locations</p><p>Choose more places or clear the location filter to see everything again.</p></div> : <EmptyList source={source.kind === "list" ? source.list : source.kind} search={search} beyond={(total ?? 0) > 0} funnel={funnel} onReload={onReload} />
						) : (
							<>
								{/* Early review always says what it is; a toolbar subtitle is too narrow to hold it whole. */}
								{source.kind === "triage" ? <p className="list-caption">{EARLY_REVIEW_CAVEAT}</p> : null}
								<CellList label={title} groups={groups} selectedKey={selectedKey} onSelect={select} keyboardMoves={keyboardMoves} />
							</>
						)}
					</div>
					{pages === null ? null : (
						<footer className="list-footer">
							<span className="tabular">
								{pages.first.toLocaleString("en-US")}–{pages.last.toLocaleString("en-US")} of {pages.total.toLocaleString("en-US")}
							</span>
							<span className="pages">
								<button type="button" className="icon-button" disabled={source.page === 0} onClick={() => navigate(sourceHash(source, source.page - 1))} aria-label="Previous page" title="Previous page">
									<ChevronLeft aria-hidden="true" className="icon" />
								</button>
								<button type="button" className="icon-button" disabled={pages.last >= pages.total} onClick={() => navigate(sourceHash(source, source.page + 1))} aria-label="Next page" title="Next page">
									<ChevronRight aria-hidden="true" className="icon" />
								</button>
							</span>
						</footer>
					)}
				</section>
				<div className="detail-pane">
					{detail.status === "error" ? (
						<div className="empty">
							<p className="empty-title">This Posting could not be read</p>
							<p>{detail.message}</p>
						</div>
					) : null}
					{current === null ? null : <PostingPage key={current.posting.key} detail={current} control={control} />}
					{awaitingKey ? <AwaitingKey waiting={total ?? keys.length} onSaved={() => setKeyToken((value) => value + 1)} /> : null}
				</div>
			</div>
			)}
		</>
	);
}
