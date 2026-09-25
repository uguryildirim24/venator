import { RotateCw, Square } from "lucide-react";
import { useState } from "react";

import type { RunKind, RunPlan, RunState } from "../../shared/runs.ts";
import { Sheet } from "../components/sheet.tsx";
import { formatMoment } from "../format.ts";
import { earlyReviewLabel, jevPauseLabel, runKindLabel, runStageLabel, runStageResultLabel } from "../labels.ts";
import { FIRST_RUN } from "../views/onboarding/copy.ts";
import { JEV_IT_KIND } from "./jev-it.ts";
import { useRunControl, type RunControl } from "./use-run.ts";

/** The button's words, exactly as Sample asked for them. */
const JEV_IT_LABEL = "Jev it";

type RunRowProps = {
	readonly kind: RunKind;
	readonly control: RunControl;
	/** The two lines shown while this kind is not running. */
	readonly idle: readonly [string, string];
	/** The press is off before anybody presses: no plan yet, or one that says there is nothing to do. */
	readonly off: boolean;
	/** A line under the row saying why the press is off, when the idle lines do not already. */
	readonly note: string | null;
	/** The press. An icon names itself with `label`; with no icon, `label` is the button's words. */
	readonly label: string;
	readonly icon: typeof RotateCw | null;
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
 * One run control in the sidebar footer: two lines of words and the press that starts it.
 *
 * Both controls sit on `useRunControl`, so neither starts anything the run panel could not: a
 * press redeems the plan token the server issued, and nothing starts on render. While this
 * kind is running the two lines become the stage in words and a bar that fills one step per
 * finished stage. That is a count of stages, not an estimate of time.
 *
 * The server runs one thing at a time, and each control reads the same current run. So a run
 * of the other kind is not shown here as this one's progress: the press waits, and says so.
 *
 * A failure is one line of words. Pressing it opens the rest — the remedy, what the stages
 * that finished left behind, and what the pipeline printed — in a sheet rather than in a
 * 240px column.
 */
function RunRow({ kind, control, idle, off, note, label, icon: Icon, updating = false }: RunRowProps) {
	const { run, busy, actionError, start, stop } = control;
	const [failureOpen, setFailureOpen] = useState(false);
	const own = run !== null && run.kind === kind ? run : null;
	const running = own !== null && own.phase === "running";
	const elsewhere = run !== null && run.kind !== kind && run.phase === "running";

	const stage = running ? (own.stages.find((entry) => entry.state === "running") ?? null) : null;
	const finished = running ? own.stages.filter((entry) => entry.state === "ok").length : 0;
	const failed = own !== null && (own.phase === "failed" || own.phase === "stopped") ? own : null;

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
								<span style={{ width: `${(finished / Math.max(1, own.stages.length)) * 100}%` }} />
							</span>
						</>
					) : (
						<>
							<span>{idle[0]}</span>
							<span>{idle[1]}</span>
						</>
					)}
				</div>
				{updating && !running ? null : running ? (
					<button type="button" className="icon-button" onClick={stop} disabled={busy} aria-label="Stop the run" title="Stop the run">
						<Square aria-hidden="true" className="icon" />
					</button>
				) : Icon === null ? (
					<button type="button" className="button" onClick={start} disabled={busy || off || elsewhere}>
						{label}
					</button>
				) : (
					<button type="button" className="icon-button" onClick={start} disabled={busy || off || elsewhere} aria-label={label} title={label}>
						<Icon aria-hidden="true" className="icon" />
					</button>
				)}
			</div>
			{running ? null : elsewhere ? <span>Waits for the run that is going</span> : note === null ? null : <span className="run-failure">{note}</span>}
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

/** The last fetch in two lines, and the icon that starts the next. */
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
	const idle: readonly [string, string] =
		lastFetchAt === null
			? ["Not checked yet", "Refresh fetches your boards"]
			: [`Checked ${boards.toLocaleString("en-US")} ${boards === 1 ? "board" : "boards"}`, formatMoment(lastFetchAt)];
	return (
		<RunRow kind={kind} control={control} idle={idle} off={plan === null || !plan.startable} note={note} label={runKindLabel(kind)} icon={RotateCw} updating={updating} />
	);
}

/**
 * What a Jev it plan says it would do, in its own words.
 *
 * The plan is the only source: this row never counts Postings itself and never estimates a
 * cost. No Jev result is shown here or anywhere else.
 */
function planWords(plan: RunPlan): string {
	if (plan.jev === null || plan.jev === undefined) return "The Jev plan did not say what it would assess";
	const { postingCount, summary } = plan.jev;
	const said = summary.trim();
	if (said !== "") return said;
	return `${postingCount.toLocaleString("en-US")} ${postingCount === 1 ? "Posting" : "Postings"} awaiting Jev`;
}

type JevItRowProps = {
	readonly kind: RunKind;
	readonly onSettled: () => void;
};

/**
 * Jev it: one press scores every Posting early review has not seen yet.
 *
 * Before the press it shows what the plan returned, in words; while it runs and after, the
 * same stage, bar and failure line the fetch shows. A plan with nothing to score turns the
 * press off, and the words beside it say so.
 */
function JevItRow({ kind, onSettled }: JevItRowProps) {
	const control = useRunControl(kind, 0, onSettled);
	const { plan, planError, run } = control;
	const words = planError !== null ? planError.message : plan === null ? "Asking what it would score…" : planWords(plan);
	const scored = run !== null && run.kind === kind && run.phase === "succeeded" && run.endedAt !== null ? run.endedAt : null;
	const idle: readonly [string, string] = [scored === null ? earlyReviewLabel() : `Scored · ${formatMoment(scored)}`, words];
	return <RunRow kind={kind} control={control} idle={idle} off={plan === null || !plan.startable} note={null} label={JEV_IT_LABEL} icon={null} />;
}

type RunStatusProps = FetchRowProps & {
	/** Whether Jev it is offered. Not before anything has been discovered. */
	readonly jevIt: boolean;
};

/**
 * The sidebar's footer: the fetch, and Jev it beneath it, drawn the same way.
 *
 * Neither is ember: a sidebar run control is not one of ember's uses in ui/DESIGN.md.
 */
export function RunStatus({ boards, lastFetchAt, onSettled, jevPause, jevIt, updating }: RunStatusProps) {
	return (
		<div className="sidebar-footer" aria-label="Runs">
			<FetchRow boards={boards} lastFetchAt={lastFetchAt} onSettled={onSettled} updating={updating} />
			{jevPause && !updating ? <span role="status">{jevPauseLabel(jevPause.reason, jevPause.waiting)}</span> : null}
			{jevIt && !updating ? <JevItRow kind={JEV_IT_KIND} onSettled={onSettled} /> : null}
		</div>
	);
}
