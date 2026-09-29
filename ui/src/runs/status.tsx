import { RotateCw, Square } from "lucide-react";
import { useState } from "react";

import type { RunKind, RunState } from "../../shared/runs.ts";
import { Sheet } from "../components/sheet.tsx";
import { TrainStatus } from "./train-status.tsx";
import { formatMoment } from "../format.ts";
import { scorePauseLabel, runKindLabel, runStageLabel, runStageResultLabel } from "../labels.ts";
import { FIRST_RUN } from "../views/onboarding/copy.ts";
import { useRunControl, type RunControl } from "./use-run.ts";

/**
 * The press that starts a run, in one of two shapes.
 *
 * Refresh is the borderless glyph beside the last fetch. Score uses the prominent capsule.
 */
type Press =
	| { readonly kind: "glyph"; readonly label: string; readonly icon: typeof RotateCw }
	| { readonly kind: "capsule"; readonly label: string; readonly prominent: boolean };

type RunRowProps = {
	readonly kind: RunKind;
	readonly control: RunControl;
	/** The line or two shown beside the press while this kind is not running. */
	readonly idle: readonly string[];
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
function RunRow({ kind, control, idle, off, note, press, updating = false }: RunRowProps) {
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
				{updating || running || idle.length > 0 ? (
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
							idle.map((line) => <span key={line}>{line}</span>)
						)}
					</div>
				) : null}
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
	/** When boards were last checked, including a CLI Discover, or null when none has. */
	readonly lastFetchAt: string | null;
	readonly onSettled: () => void;
	readonly updating: boolean;
};

/** The one-line check under Employers, with its Refresh press. */
export function FetchStatus({ lastFetchAt, onSettled, updating }: FetchRowProps) {
	const kind: RunKind = "fetch-and-filter";
	const control = useRunControl(kind, 0, onSettled);
	const { plan, planError } = control;
	const note = planError?.message ?? null;
	const moment = lastFetchAt === null ? null : formatMoment(lastFetchAt);
	const idle = [moment === null ? "Not checked yet" : `Checked ${moment.charAt(0).toLowerCase()}${moment.slice(1)}`];
	return (
		<div className="sidebar-check">
			<RunRow
				kind={kind}
				control={control}
				idle={idle}
				off={plan === null || !plan.startable}
				note={note}
				press={{ kind: "glyph", label: runKindLabel(kind), icon: RotateCw }}
				updating={updating}
			/>
		</div>
	);
}

type ScoreStatusProps = {
	readonly scorePause: { readonly reason: string; readonly waiting: number } | null;
	readonly onSettled: () => void;
};

export function ScoreStatus({ scorePause, onSettled }: ScoreStatusProps) {
	const control = useRunControl("score", 0, onSettled);
	const { plan, planError, run } = control;
	const sorted = run?.kind === "score" && run.phase === "succeeded" ? run.endedAt : null;
	const note = planError?.message ?? (scorePause !== null ? scorePauseLabel(scorePause.reason) : sorted !== null ? `Sorted ${formatMoment(sorted)}` : null);
	return (
		<>
			<RunRow
				kind="score"
				control={control}
				idle={[]}
				off={plan === null || !plan.startable}
				note={note}
				press={{ kind: "capsule", label: runKindLabel("score"), prominent: true }}
			/>
			<TrainStatus onSettled={onSettled} />
		</>
	);
}
