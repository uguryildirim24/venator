import { KeyRound, LoaderCircle, Search } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode, type RefObject } from "react";

import type { BoardEntry } from "../../shared/onboarding.ts";
import { ExternalLink } from "../components/external-link.tsx";
import { Sheet, SheetClose } from "../components/sheet.tsx";
import { Toolbar, type SearchProps } from "../components/toolbar.tsx";
import { homeListLabel } from "../labels.ts";
import { OnboardingRequestError, readInstallSettings, registerEmployers } from "../onboarding/api.ts";
import { navigate, onboardingHash, queueHash, replaceRoute, type HomeList } from "../router.ts";
import { planRun } from "../runs/api.ts";
import { useRunControl } from "../runs/use-run.ts";
import { FIRST_RUN, keyBody, LINKS, refreshBody, RESUME } from "./onboarding/copy.ts";
import { EmployerPicker } from "./onboarding/employers.tsx";
import { JevKeyField } from "./onboarding/parts.tsx";

/**
 * The three first-run states (ui/DESIGN.md, "Setup and first run"), drawn in the main window
 * with its sidebar rather than over it:
 *
 * - **No jobs yet** — a Profile and nothing discovered: Refresh, or the reason it can't run.
 * - **Awaiting Jev, no key** — Postings wait for Jev and there is no TypeSafe key to run it.
 * - **Updating your job list** — the view is being rebuilt under a read that is still open
 *   (`server/view-recovery.ts`), so the list is on its way rather than missing.
 */

/** A first-run state's glyph, title, sentence and presses, centred where the list would be. */
function State({ glyph, title, body, children }: {
	readonly glyph: "search" | "key" | "spinner";
	readonly title: string;
	readonly body: string | null;
	readonly children?: ReactNode;
}) {
	return (
		<div className="first-run" role={glyph === "spinner" ? "status" : undefined}>
			{glyph === "search" ? <Search aria-hidden="true" className="first-run-glyph" /> : null}
			{glyph === "key" ? <KeyRound aria-hidden="true" className="first-run-glyph" /> : null}
			{glyph === "spinner" ? <LoaderCircle aria-hidden="true" className="first-run-glyph spinner" /> : null}
			<p className="first-run-title">{title}</p>
			{body === null ? null : <p className="first-run-body">{body}</p>}
			{children === undefined ? null : <div className="first-run-actions">{children}</div>}
		</div>
	);
}

/** Updating your job list: the view is being rebuilt and the read will answer when it is. */
export function UpdatingState() {
	return <State glyph="spinner" title={FIRST_RUN.updatingTitle} body={FIRST_RUN.updatingBody} />;
}

/**
 * Registers employers into the Profile that already exists, from a sheet.
 *
 * The rescue for a Profile with no employer: Discover asks only the boards a Profile
 * registers, so without one Refresh has nothing to fetch. The picker is setup's own.
 */
function AddEmployersSheet({ profile, open, onOpenChange, onRegistered }: {
	readonly profile: string;
	readonly open: boolean;
	readonly onOpenChange: (open: boolean) => void;
	readonly onRegistered: () => void;
}) {
	const [boards, setBoards] = useState<readonly BoardEntry[]>([]);
	const [sending, setSending] = useState(false);
	const [refusal, setRefusal] = useState<OnboardingRequestError | null>(null);
	const send = useCallback(() => {
		if (boards.length === 0 || sending) return;
		setSending(true);
		setRefusal(null);
		registerEmployers(profile, boards.map((board) => ({ source: board.source, board: board.board, name: board.name })))
			.then(() => {
				setBoards([]);
				onOpenChange(false);
				onRegistered();
			})
			.catch((error: Error) => setRefusal(error instanceof OnboardingRequestError ? error : new OnboardingRequestError(error.message, null, null, null)))
			.finally(() => setSending(false));
	}, [boards, onOpenChange, onRegistered, profile, sending]);
	return (
		<Sheet
			open={open}
			onOpenChange={onOpenChange}
			title={FIRST_RUN.employersSheetTitle}
			description={FIRST_RUN.employersSheetDescription}
			actions={
				<>
					<SheetClose className="button" data-size="large">
						{RESUME.cancel}
					</SheetClose>
					<button type="button" className="button" data-size="large" data-tone="prominent" disabled={boards.length === 0 || sending} onClick={send}>
						{FIRST_RUN.register}
					</button>
				</>
			}
		>
			<EmployerPicker boards={boards} onChange={setBoards} />
			{refusal === null ? null : (
				<div className="setup-alert" role="alert">
					<p>{refusal.message}</p>
					{refusal.remedy === null ? null : <p>{refusal.remedy}</p>}
				</div>
			)}
		</Sheet>
	);
}

type NoJobsYetProps = {
	readonly list: HomeList;
	/** The Profile a run would use, or null when this Install has none to run. */
	readonly profile: string | null;
	readonly searchField: RefObject<HTMLInputElement | null>;
	readonly sidebarHidden: boolean;
	readonly onShowSidebar: () => void;
	readonly onSettled: () => void;
};

/**
 * No jobs yet: nothing has been discovered, and the next thing to press is Refresh.
 *
 * Refresh is the sidebar's run, redeemed from the same plan. When the plan says a run could not
 * start, the screen says why instead — no Profile, no employers (with the rescue), or the
 * plan's own note — so the press is never simply dead.
 */
export function NoJobsYet({ list, profile, searchField, sidebarHidden, onShowSidebar, onSettled }: NoJobsYetProps) {
	const [token, setToken] = useState(0);
	const [employersOpen, setEmployersOpen] = useState(false);
	const { plan, planError, run, busy, actionError, start } = useRunControl("fetch-and-filter", token, onSettled);
	const running = run?.phase === "running";
	const failed = run !== null && run.phase !== "running" && run.phase !== "succeeded" ? run.failure : null;
	const search: SearchProps = {
		value: "",
		placeholder: "Search",
		onCommit: (next) => replaceRoute(queueHash(list, next)),
		field: searchField,
	};

	let state: ReactNode;
	if (profile === null) {
		state = (
			<State glyph="search" title={FIRST_RUN.noJobs} body={FIRST_RUN.noJobsSetUp}>
				<button type="button" className="button" data-size="large" data-tone="prominent" onClick={() => navigate(onboardingHash())}>
					{FIRST_RUN.setUp}
				</button>
			</State>
		);
	} else if (running) {
		state = <State glyph="search" title={FIRST_RUN.noJobs} body={FIRST_RUN.checking} />;
	} else if (plan !== null && plan.boards === 0) {
		state = (
			<State glyph="search" title={FIRST_RUN.noJobs} body={FIRST_RUN.noEmployers}>
				<button type="button" className="button" data-size="large" data-tone="prominent" onClick={() => setEmployersOpen(true)}>
					{FIRST_RUN.addEmployers}
				</button>
			</State>
		);
	} else {
		const body =
			planError !== null
				? planError.message
				: plan === null
					? null
					: plan.startable
						? refreshBody(plan.boards)
						: (plan.notes[0]?.message ?? null);
		state = (
			<State glyph="search" title={FIRST_RUN.noJobs} body={body}>
				<button type="button" className="button" data-size="large" data-tone="prominent" disabled={busy || plan === null || !plan.startable} onClick={start}>
					{FIRST_RUN.refresh}
				</button>
			</State>
		);
	}

	return (
		<>
			<Toolbar title={homeListLabel(list)} subtitle={FIRST_RUN.noJobs} search={search} sidebarHidden={sidebarHidden} onShowSidebar={onShowSidebar} />
			{state}
			{failed === null && actionError === null ? null : (
				<p className="first-run-body" role="alert">
					{actionError?.message ?? failed?.message}
				</p>
			)}
			{profile === null ? null : (
				<AddEmployersSheet
					profile={profile}
					open={employersOpen}
					onOpenChange={setEmployersOpen}
					onRegistered={() => {
						setToken((value) => value + 1);
						onSettled();
					}}
				/>
			)}
		</>
	);
}

/**
 * Whether Jev is waiting for a key, as the Jev plan says it.
 *
 * Asked only while the Awaiting Jev list is on screen. The plan is the server's answer — it
 * knows whether a key is in the environment or the Keychain — and asking writes nothing.
 */
export function useJevKeyMissing(active: boolean, token: number): boolean {
	const [missing, setMissing] = useState(false);
	useEffect(() => {
		if (!active) {
			setMissing(false);
			return;
		}
		let cancelled = false;
		planRun("jev-it")
			.then((answer) => {
				if (!cancelled) setMissing(answer.plan.notes.some((note) => note.code === "jev-key-missing"));
			})
			.catch(() => {
				if (!cancelled) setMissing(false);
			});
		return () => {
			cancelled = true;
		};
	}, [active, token]);
	return missing;
}

/** Awaiting Jev, no key: the Postings wait, and the key is one sheet away. */
export function AwaitingKey({ waiting, onSaved }: { readonly waiting: number; readonly onSaved: () => void }) {
	const [open, setOpen] = useState(false);
	const [present, setPresent] = useState(false);
	useEffect(() => {
		if (!open) return;
		readInstallSettings()
			.then((settings) => setPresent(settings.jevKeyPresent))
			.catch(() => setPresent(false));
	}, [open]);
	return (
		<>
			<State glyph="key" title={FIRST_RUN.keyTitle} body={keyBody(waiting)}>
				<button type="button" className="button" data-size="large" data-tone="prominent" onClick={() => setOpen(true)}>
					{FIRST_RUN.addKey}
				</button>
				<ExternalLink href={LINKS.typesafeKeys} className="button" size="large" title={LINKS.typesafeKeys}>
					{FIRST_RUN.getKey}
				</ExternalLink>
			</State>
			<Sheet
				open={open}
				onOpenChange={setOpen}
				title={FIRST_RUN.keySheetTitle}
				description={FIRST_RUN.keySheetDescription}
				actions={
					<SheetClose className="button" data-size="large">
						{RESUME.cancel}
					</SheetClose>
				}
			>
				<JevKeyField
					present={present}
					onSaved={() => {
						setOpen(false);
						onSaved();
					}}
				/>
			</Sheet>
		</>
	);
}
