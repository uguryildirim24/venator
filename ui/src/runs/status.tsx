import { RotateCw, Square } from "lucide-react";
import { useState } from "react";

import type { RunKind, RunState } from "../../shared/runs.ts";
import { Sheet } from "../components/sheet.tsx";
import { formatMoment } from "../format.ts";
import { jevPauseLabel, runKindLabel, runStageLabel, runStageResultLabel } from "../labels.ts";
import { FIRST_RUN } from "../views/onboarding/copy.ts";
import { JEV_IT_KIND } from "./jev-it.ts";
import { useRunControl, type RunControl } from "./use-run.ts";

/** The button's words, exactly as Sample asked for them. */
const JEV_IT_LABEL = "Jev it";

/**
 * The press that starts a run, in one of the kit's two shapes (ui/DESIGN.md, "Sidebar").
 *
 * Refresh is the borderless glyph the mock draws beside the last fetch. Jev it is the large
 * capsule every prominent press in the app is — Continue, Find Jobs, Add Key — and it is
 * ember while there is something to sort and a key to sort it with. With no key it is the
 * same capsule in grey: the press still works, and pauses cleanly, but Add Key… is then the
 * one ember press on the screen.
 */
type Press =
	| { readonly kind: "glyph"; readonly label: string; readonly icon: typeof RotateCw }
	| { readonly kind: "capsule"; readonly label: string; readonly prominent: boolean };

type RunRowProps = {
	readonly kind: RunKind;
	readonly control: RunControl;
	/** The line or two shown beside the press while this kind is not running. */
	readonly idle: readonly string[];
	/** The plan's own sentence in full, as the idle lines' tooltip. */
	readonly idleTitle?: string | undefined;
	/** The press is off before anybody presses: no plan yet, or one that says there is nothing to do. */
	readonly off: boolean;
	/** A line under the row, in words: why the press is off, the last pause, the last run. */
	readonly note: string | null;
	readonly press: Press;
	/** The job list is being rebuilt: say so, with a bar that only moves, and hold the press. */
	readonly updating?: boolean | undefined;
};

/**
 * What a run that did not finish still left behind, and what the pipeline printed.
 *
 * The stages that reached `ok` wrote what they write, and `data/` is append-only, so this is
 * a statement of fact rather than reassurance. Starting the run again is safe: Discover skips
 * Postings already stored, and the Hard Filters append nothing for a Posting whose standing
 * decision still says the same thing.
 */
function RunFailureDetail({ run }: { readonly run: RunState }) {
	if (run.failure === null) return null;
	const finished = run.stages.filter((entry) => entry.state === "ok");
	return (
		<div className="sheet-notes">
			{run.failure.remedy === null ? null : <p>{run.failure.remedy}</p>}
			{finished.map((entry) => (
				<p key={entry.stage} className="form-help">
					{runStageResultLabel(entry.stage)}
				</p>
			))}
			{run.viewRecovered ? (
				<p className="form-help">The dashboard was brought up to date anyway, so what it did record is visible.</p>
			) : null}
			{run.failure.transcript === null ? null : (
				<details>
					<summary className="form-help">What the pipeline printed</summary>
					<pre className="sheet-text">{run.failure.transcript}</pre>
				</details>
			)}
		</div>
	);
}

/**
 * One run control in the sidebar footer: its lines in words, and the press that starts it.
 *
 * Both controls sit on `useRunControl`, so neither starts anything the run panel could not: a
 * press redeems the plan token the server issued, and nothing starts on render. While this
 * kind is running the lines become the stage in words and a bar that fills one step per
 * finished stage. That is a count of stages, not an estimate of time.
 *
 * The server runs one thing at a time, and each control reads the same current run. So a run
 * of the other kind is not shown here as this one's progress: the press waits, and says so.
 *
 * A failure is one line of words. Pressing it opens the rest — the remedy, what the stages
 * that finished left behind, and what the pipeline printed — in a sheet rather than in a
 * 240px column.
 */
function RunRow({ kind, control, idle, idleTitle, off, note, press, updating = false }: RunRowProps) {
	const { run, busy, actionError, start, stop } = control;
	const [failureOpen, setFailureOpen] = useState(false);
	const own = run !== null && run.kind === kind ? run : null;
	const running = own !== null && own.phase === "running";
	const elsewhere = run !== null && run.kind !== kind && run.phase === "running";

	const stage = running ? (own.stages.find((entry) => entry.state === "running") ?? null) : null;
	const finished = running ? own.stages.filter((entry) => entry.state === "ok").length : 0;
	const failed = own !== null && (own.phase === "failed" || own.phase === "stopped") ? own : null;
	// A press in flight does not hold the button: `start` ignores a second press on its own, and
	// disabling here would flash the capsule grey for the moment before the run appears.
	const held = off || elsewhere;

	return (
		<div className="run-row">
			<div className="run-status">
				<div className="run-status-lines" aria-live="polite">
					{updating && !running ? (
						<>
							<span>{FIRST_RUN.updatingSidebar}</span>
							<span className="run-progress" data-indeterminate="" role="progressbar" aria-label={FIRST_RUN.updatingSidebar}>
								<span />
							</span>
						</>
					) : running ? (
						<>
							<span>{stage === null ? "Starting…" : `${runStageLabel(stage.stage)}…`}</span>
							<span className="run-progress" role="progressbar" aria-valuemin={0} aria-valuemax={own.stages.length} aria-valuenow={finished}>
								<span style={{ transform: `scaleX(${String(finished / Math.max(1, own.stages.length))})` }} />
							</span>
						</>
					) : (
						idle.map((line) => (
							<span key={line} title={idleTitle}>
								{line}
							</span>
						))
					)}
				</div>
				{updating && !running ? null : running ? (
					<button type="button" className="icon-button" onClick={stop} disabled={busy} aria-label="Stop the run" title="Stop the run">
						<Square aria-hidden="true" className="icon" />
					</button>
				) : press.kind === "capsule" ? (
					<button type="button" className="button" data-size="large" data-tone={press.prominent && !held ? "prominent" : undefined} onClick={start} disabled={held}>
						{press.label}
					</button>
				) : (
					<button type="button" className="icon-button" onClick={start} disabled={held} aria-label={press.label} title={press.label}>
						<press.icon aria-hidden="true" className="icon" />
					</button>
				)}
			</div>
			{running ? null : elsewhere ? <span className="run-note">Waits for the run that is going</span> : note === null ? null : <span className="run-note">{note}</span>}
			{actionError === null ? null : <span className="run-failure">{actionError.message}</span>}
			{running || failed === null || failed.failure === null ? null : (
				<>
					<button type="button" className="run-failure link-button" onClick={() => setFailureOpen(true)}>
						{failed.failure.message}
					</button>
					<Sheet open={failureOpen} onOpenChange={setFailureOpen} title={failed.failure.message}>
						<RunFailureDetail run={failed} />
					</Sheet>
				</>
			)}
		</div>
	);
}

type FetchRowProps = {
	/** How many boards the last fetch checked: one source-health row each. */
	readonly boards: number;
	/** When the latest completed fetch finished, or null when none has. */
	readonly lastFetchAt: string | null;
	readonly onSettled: () => void;
	readonly jevPause?: { readonly reason: string; readonly waiting: number } | null;
	readonly updating: boolean;
};

/** The last fetch in two lines, and the glyph that starts the next. */
function FetchRow({ boards, lastFetchAt, onSettled, updating }: FetchRowProps) {
	const kind: RunKind = "fetch-and-filter";
	const control = useRunControl(kind, 0, onSettled);
	const { plan, planError } = control;
	const note =
		planError !== null
			? planError.message
			: plan !== null && !plan.startable
				? (plan.notes[0]?.message ?? "There is nothing for a run to fetch yet.")
				: null;
	const idle: readonly string[] =
		lastFetchAt === null
			? ["Not checked yet", "Refresh fetches your boards"]
			: [`Checked ${boards.toLocaleString("en-US")} ${boards === 1 ? "board" : "boards"}`, formatMoment(lastFetchAt)];
	return (
		<RunRow
			kind={kind}
			control={control}
			idle={idle}
			off={plan === null || !plan.startable}
			note={note}
			press={{ kind: "glyph", label: runKindLabel(kind), icon: RotateCw }}
			updating={updating}
		/>
	);
}

/** How many Postings the plan would send to Jev, as the one line beside the press. */
function awaitingLabel(count: number): string {
	if (count === 0) return "Nothing awaiting Jev";
	return `${count.toLocaleString("en-US")} awaiting Jev`;
}

type JevItRowProps = {
	readonly kind: RunKind;
	/** The last Jev it pause the view recorded, if the latest Jev run ended in one. */
	readonly jevPause: { readonly reason: string; readonly waiting: number } | null;
	readonly onSettled: () => void;
};

/**
 * Jev it: one press sorts every Posting that passed the Hard Filters and has no Jev result.
 *
 * The line beside the press is the plan's count, and the plan's full sentence is its tooltip.
 * This row never counts Postings itself and never estimates a cost, and no Jev result is
 * shown here or anywhere else. Under it, one note at most: the last pause and what resumes
 * it, the plan's word on a missing key, or when the last sort finished. A plan with nothing
 * to sort turns the press off and the line says so.
 */
function JevItRow({ kind, jevPause, onSettled }: JevItRowProps) {
	const control = useRunControl(kind, 0, onSettled);
	const { plan, planError, run } = control;
	const count = plan?.jev?.postingCount ?? null;
	const keyMissing = plan?.notes.find((entry) => entry.code === "jev-key-missing") ?? null;
	const sorted = run !== null && run.kind === kind && run.phase === "succeeded" && run.endedAt !== null ? run.endedAt : null;
	const line =
		planError !== null
			? "Jev could not be planned"
			: plan === null
				? "Counting what awaits Jev…"
				: count === null
					? "The Jev plan did not say what it would assess"
					: awaitingLabel(count);
	const note =
		planError !== null
			? planError.message
			: jevPause !== null
				? jevPauseLabel(jevPause.reason)
				: keyMissing !== null
					? keyMissing.message
					: sorted !== null
						? `Sorted ${formatMoment(sorted)}`
						: null;
	const off = plan === null || !plan.startable;
	return (
		<RunRow
			kind={kind}
			control={control}
			idle={[line]}
			idleTitle={plan?.jev?.summary}
			off={off}
			note={note}
			press={{ kind: "capsule", label: JEV_IT_LABEL, prominent: keyMissing === null }}
		/>
	);
}

type RunStatusProps = FetchRowProps & {
	/** Whether Jev it is offered. Not before anything has been discovered. */
	readonly jevIt: boolean;
};

/**
 * The sidebar's footer: the fetch, and Jev it beneath it.
 *
 * Refresh stays the borderless glyph the mock draws; Jev it is the footer's one prominent
 * press, ember once there is something to sort and a key to sort it with (ui/DESIGN.md,
 * principle 2).
 */
export function RunStatus({ boards, lastFetchAt, onSettled, jevPause, jevIt, updating }: RunStatusProps) {
	return (
		<div className="sidebar-footer" aria-label="Runs">
			<FetchRow boards={boards} lastFetchAt={lastFetchAt} onSettled={onSettled} updating={updating} />
			{jevIt && !updating ? <JevItRow kind={JEV_IT_KIND} jevPause={jevPause ?? null} onSettled={onSettled} /> : null}
		</div>
	);
}
