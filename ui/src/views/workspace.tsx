import { ChevronLeft, ChevronRight } from "lucide-react";
import { useCallback, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";

import type { Funnel, PostingEntry } from "../../shared/contracts.ts";
import { useLocations, usePostingDetail, usePostings } from "../api.ts";
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
	homeListLabel,
	hiddenKeepLabel,
	originLabel,
	postingCountLabel,
	ruleLabel,
} from "../labels.ts";
import { homeListOf, homeListQuery, inspectorQuery } from "../lists.ts";
import {
	inspectorHash,
	navigate,
	PAGE_SIZE,
	parseRoute,
	postingHash,
	queueHash,
	replaceRoute,
	type FocusFrom,
	type HomeList,
	type InspectorFilters,
	type Route,
} from "../router.ts";
import { EmptyList } from "./empty-list.tsx";
import { UpdatingState } from "./first-run.tsx";
import { FIRST_RUN } from "./onboarding/copy.ts";
import { nextPostingArrival } from "./posting-arrival.ts";
import { latestHardFilter, scoreHidden, triageAllowed } from "../triage.ts";

/** What the list column is showing: a home list or the inspector's results. */
type Source =
	| { readonly kind: "list"; readonly list: HomeList; readonly search: string; readonly page: number }
	| { readonly kind: "inspector"; readonly filters: InspectorFilters; readonly page: number };

function sourceOf(route: Route): Source {
	if (route.name === "queue") return { kind: "list", list: route.list, search: route.search, page: route.page ?? 0 };
	if (route.name === "posting") {
		if (route.returnTo !== undefined) {
			const back = parseRoute(route.returnTo);
			if (back.name === "queue" || back.name === "inspector") return sourceOf(back);
		}
		return { kind: "list", list: route.from, search: "", page: 0 };
	}
	return { kind: "list", list: "queued", search: "", page: 0 };
}

function sourceHash(source: Source, page = source.page): string {
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
		status = { text: scoreHidden(entry.hardFilter, entry.keepProbability) && entry.keepProbability !== null ? hiddenKeepLabel(entry.keepProbability) : entry.hardFilter?.rule ? ruleLabel(entry.hardFilter.rule) : homeListLabel("filtered"), glyph: "excluded" };
	} else if (list === "applied" && state !== null) {
		status = {
			text: `${applicationStateLabel(state)}${entry.application?.since ? ` · ${formatDay(entry.application.since)}` : ""}`,
			glyph: state === "prepared" ? "ready" : null,
		};
	} else if (state === "prepared") {
		status = { text: "Documents ready", glyph: "ready" };

	}
	return {
		key: posting.key,
		title: posting.title,
		count: entry.groupKeys?.length ?? 1,
		groupKeys: entry.groupKeys ?? [posting.key],
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
 * `#/queue` shows its first Posting; choosing another replaces the route with
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

	const listState = postings;
	const keys = useMemo(() => postings.status === "ready" ? postings.value.entries.map((entry) => entry.posting.key) : [], [postings]);
	const groups = useMemo(() => postings.status === "ready" ? dayGroups(postings.value.entries, source.kind === "list" ? source.list : null) : [], [source, postings]);

	const selectedKey = route.name === "posting" && (!locationFiltered || listState.status !== "ready" || (postings.status === "ready" && postings.value.entries.some((entry) => entry.groupKeys?.includes(route.key)))) ? route.key : (keys[0] ?? null);
	const detailPane = useRef<HTMLDivElement>(null);
	const [arrival, setArrival] = useState(() => ({ key: selectedKey, at: performance.now(), animate: false }));
	useLayoutEffect(() => {
		// Reset the reading position on selection, not after the new detail finishes loading.
		if (detailPane.current !== null) detailPane.current.scrollTop = 0;
		setArrival((previous) => nextPostingArrival(previous, selectedKey, performance.now()));
	}, [selectedKey]);
	const detail = usePostingDetail(selectedKey, reloadToken);
	const control = useApplicationControl(selectedKey, reloadToken, onReload);
	const [keyboardMoves, setKeyboardMoves] = useState(0);

	const from: FocusFrom = source.kind === "list" ? source.list : "queued";
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
			const head = postings.status === "ready" ? postings.value.entries.find((entry) => entry.groupKeys?.includes(selectedKey ?? ""))?.posting.key : undefined;
			const index = head === undefined ? -1 : keys.indexOf(head);
			const next = keys[Math.max(0, Math.min(keys.length - 1, to(index)))];
			if (next === undefined) return;
			select(next);
			setKeyboardMoves((count) => count + 1);
		},
		[keys, postings, select, selectedKey],
	);
	const selectedEntry = postings.status === "ready" ? postings.value.entries.find((entry) => entry.groupKeys?.includes(selectedKey ?? "")) : undefined;
	const current = detail.status === "ready" && detail.value.posting.key === selectedKey ? detail.value : null;
	const selectedPosting = current ?? (selectedEntry?.posting.key === selectedKey ? selectedEntry : undefined);
	const selectedFilter = current === null ? selectedEntry?.hardFilter ?? null : latestHardFilter(current);
	const canTriage = selectedPosting !== undefined && triageAllowed(selectedPosting, selectedFilter) && control.busy === null;
	const bindings = useMemo<readonly KeyBinding[]>(
		() => [
			{ keys: ["j", "ArrowDown"], label: "j", description: "next Posting", run: () => move((index) => index + 1) },
			{ keys: ["k", "ArrowUp"], label: "k", description: "previous Posting", run: () => move((index) => index - 1) },
			{ keys: ["g"], label: "g", description: "first Posting", run: () => move(() => 0) },
			{ keys: ["G"], label: "G", description: "last Posting", run: () => move(() => keys.length - 1) },
			{ keys: ["s"], label: "s", description: "Save Posting", run: () => {
				if (canTriage && (selectedPosting?.application?.state === null || selectedPosting?.application?.state === undefined || selectedPosting.application.state === "queued")) control.run("save");
			} },
			{ keys: ["x"], label: "x", description: "Dismiss Posting", run: () => {
				if (canTriage && selectedPosting?.application?.state !== "rejected") control.run("dismiss");
			} },
		],
		[canTriage, control, keys.length, move, selectedPosting],
	);
	useKeyBindings(bindings);

	const total = listState.status === "ready" ? listState.value.total : null;
	const fresh = postings.status === "ready" ? postings.value.newCount : 0;
	const title = source.kind === "inspector" ? "Filter inspector" : homeListLabel(source.list);
	const search = source.kind === "inspector" ? source.filters.search : source.search;
	const updating = useDelayed(listState.status === "loading");
	const subtitle =
		listState.status === "loading"
			? FIRST_RUN.updatingToolbar
			: total === null
			? null
			: search !== ""
				? `${total.toLocaleString("en-US")} matching “${search}”`
				: postingCountLabel(total, fresh);

	const commitSearch = useCallback(
		(next: string) => {
			if (source.kind === "inspector") replaceRoute(inspectorHash({ ...source.filters, search: next }));
			else replaceRoute(queueHash(source.list, next));
		},
		[source],
	);
	const searchProps: SearchProps = { value: search, placeholder: "Search", onCommit: commitSearch, field: searchField };

	const shown = postings.status === "ready" ? postings.value.entries.length : 0;
	const pages = total === null || total <= PAGE_SIZE ? null : { first: offset + 1, last: offset + shown, total };
	const requisitions = postings.status === "ready" ? postings.value.entries.find((entry) => entry.groupKeys?.includes(selectedKey ?? ""))?.groupKeys ?? [] : [];

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
				<div className="detail-pane" ref={detailPane}>
					{detail.status === "error" ? (
						<div className="empty">
							<p className="empty-title">This Posting could not be read</p>
							<p>{detail.message}</p>
						</div>
					) : null}
					{requisitions.length > 1 ? <nav aria-label="Requisitions" className="requisition-links">{requisitions.map((key, index) => <button type="button" key={key} aria-current={key === selectedKey ? "page" : undefined} onClick={() => select(key)}>Requisition {index + 1}</button>)}</nav> : null}
					{current === null ? null : <PostingPage key={current.posting.key} detail={current} control={control} arriving={arrival.key === selectedKey && arrival.animate} />}
				</div>
			</div>
			)}
		</>
	);
}
