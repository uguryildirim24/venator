import { useEffect, useMemo, useRef, useState } from "react";

import type { DiscoveredProfile, OnboardingStateResponse, ProfileWriteResponse } from "../shared/onboarding.ts";
import { useOnboardingState, useReloadToken, useSummary } from "./api.ts";
import { HelpSheet } from "./components/help.tsx";
import { Sheet } from "./components/sheet.tsx";
import { Sidebar } from "./components/sidebar.tsx";
import { useDelayed } from "./delayed.ts";
import { useKeyBindings, type KeyBinding } from "./keys.ts";
import { nativeClasses } from "./platform.ts";
import {
	EMPTY_FILTERS,
	inspectorHash,
	navigate,
	onboardingHash,
	queueHash,
	replaceRoute,
	triageHash,
	useRoute,
} from "./router.ts";
import { SHORTCUTS } from "./shortcuts.ts";
import { EmployersView } from "./views/employers.tsx";
import { NoJobsYet } from "./views/first-run.tsx";
import { InspectorView } from "./views/inspector.tsx";
import { SAVED_SHEET } from "./views/onboarding/copy.ts";
import { OnboardingFlow } from "./views/onboarding/flow.tsx";
import { Workspace } from "./views/workspace.tsx";

/** The Profile a run would use: the only one that is not a scaffold, or nothing. */
function impliedProfile(state: OnboardingStateResponse | null): DiscoveredProfile | null {
	if (state === null || state.implied === null) return null;
	return state.profiles.find((profile) => profile.name === state.implied) ?? null;
}

export function App() {
	const route = useRoute();
	const [reloadToken, reload] = useReloadToken();
	const summary = useSummary(reloadToken);
	const [installToken, rereadInstall] = useReloadToken();
	const install = useOnboardingState(installToken);
	const [helpOpen, setHelpOpen] = useState(false);
	const [sidebarHidden, setSidebarHidden] = useState(false);
	const [saved, setSaved] = useState<ProfileWriteResponse | null>(null);
	const searchField = useRef<HTMLInputElement | null>(null);

	const installState = install.status === "ready" ? install.value : null;

	/**
	 * The first-run redirect, and its two limits.
	 *
	 * An Install with no Profile that is not a scaffold has nothing of the Owner's to show, so
	 * opening the app lands on setup. But it only ever fires when they opened the app with no
	 * route at all: someone who followed a link to a Posting, or typed a route, asked for
	 * something and is not pulled off it. And it fires once — `sent` makes sure a person who
	 * leaves setup deliberately is not dragged back the moment the state is re-read. Every
	 * setup screen carries an exit, so nobody is held here either way.
	 */
	const sent = useRef(false);
	useEffect(() => {
		if (sent.current || installState === null || installState.configured) return;
		const hash = window.location.hash;
		if (hash !== "" && hash !== "#" && hash !== "#/") return;
		sent.current = true;
		replaceRoute(onboardingHash());
	}, [installState]);

	const bindings = useMemo<readonly KeyBinding[]>(
		() => [
			{ keys: ["1"], label: "1", description: "For you", run: () => navigate(queueHash()) },
			{ keys: ["2"], label: "2", description: "filter inspector", run: () => navigate(inspectorHash(EMPTY_FILTERS)) },
			{ keys: ["3"], label: "3", description: "early review", run: () => navigate(triageHash()) },
			{ keys: ["/"], label: "/", description: "search", run: () => searchField.current?.focus() },
			{ keys: ["r"], label: "r", description: "reload", run: reload },
			{ keys: ["?"], label: "?", description: "keyboard shortcuts", run: () => setHelpOpen((open) => !open) },
			{
				keys: ["Escape"],
				label: "esc",
				description: "leave the search, or clear it",
				run: () => {
					if (document.activeElement === searchField.current) {
						searchField.current?.blur();
						return;
					}
					if (route.name === "queue" && route.search !== "") replaceRoute(queueHash(route.list));
					if (route.name === "triage" && route.search !== "") replaceRoute(triageHash(route.decision));
					if (route.name === "inspector" && route.filters.search !== "") replaceRoute(inspectorHash({ ...route.filters, search: "" }));
				},
			},
		],
		[reload, route],
	);
	useKeyBindings(bindings);

	const funnel = summary.status === "ready" ? summary.value.funnel : null;
	const database = summary.status === "ready" ? summary.value.database : null;
	const boards = summary.status === "ready" ? (summary.value.sources?.length ?? 0) : 0;
	const updating = useDelayed(summary.status === "loading");
	const firstRun = funnel !== null && funnel.discovered === 0;
	const implied = impliedProfile(installState);
	const help = <HelpSheet shortcuts={SHORTCUTS} open={helpOpen} onOpenChange={setHelpOpen} />;
	/** What a Profile write said worth reading, once setup hands over to the window. */
	const savedSheet = (
		<Sheet
			open={saved !== null && saved.warnings.length > 0}
			onOpenChange={(open) => {
				if (!open) setSaved(null);
			}}
			title={SAVED_SHEET.title}
			description={SAVED_SHEET.description}
		>
			<ul className="sheet-notes">
				{(saved?.warnings ?? []).map((warning) => (
					<li key={warning} className="sheet-description">
						{warning}
					</li>
				))}
			</ul>
		</Sheet>
	);

	/**
	 * Setting up Venator owns the whole window: a title, no sidebar, no toolbar. It is
	 * reachable whether or not this Install is configured — Profile in the sidebar leads back.
	 */
	if (route.name === "onboarding") {
		return (
			<div className={`window-bleed ${nativeClasses}`}>
				<OnboardingFlow
					step={route.step}
					implied={installState?.implied ?? null}
					onWritten={(response) => {
						setSaved(response);
						rereadInstall();
						reload();
					}}
				/>
				{help}
			</div>
		);
	}

	const showSidebar = () => setSidebarHidden(false);
	return (
		<div className={`window ${nativeClasses}`} data-sidebar={sidebarHidden ? "hidden" : undefined}>
			<Sidebar
				route={route}
				funnel={funnel}
				database={database}
				boards={boards}
				configured={installState?.configured === true}
				firstRun={firstRun}
				updating={updating}
				onToggle={() => setSidebarHidden(true)}
				onReload={reload}
			/>
			<main className="window-main">
				{route.name === "queue" && firstRun ? (
					<NoJobsYet
						list={route.list}
						profile={implied?.name ?? null}
						searchField={searchField}
						sidebarHidden={sidebarHidden}
						onShowSidebar={showSidebar}
						onSettled={reload}
					/>
				) : route.name === "inspector" ? (
					<InspectorView
						filters={route.filters}
						page={route.page ?? 0}
						funnel={funnel}
						reloadToken={reloadToken}
						searchField={searchField}
						sidebarHidden={sidebarHidden}
						onShowSidebar={showSidebar}
					/>
				) : route.name === "employers" ? (
					<EmployersView summary={summary} sidebarHidden={sidebarHidden} onShowSidebar={showSidebar} />
				) : (
					<Workspace
						route={route}
						reloadToken={reloadToken}
						onReload={reload}
						funnel={funnel}
						searchField={searchField}
						sidebarHidden={sidebarHidden}
						onShowSidebar={showSidebar}
					/>
				)}
				{/*
				 * The whole summary failed to read, so every count and the source health are
				 * absent rather than wrong. It sits under the route rather than replacing it: the
				 * route may still have read its own list.
				 */}
				{summary.status === "error" ? (
					<div className="banner" role="alert">
						The dashboard could not read the view database.
						<p>{summary.message}</p>
					</div>
				) : null}
			</main>
			{help}
			{savedSheet}
		</div>
	);
}
