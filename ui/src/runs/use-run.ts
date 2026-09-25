/**
 * The state behind the run controls: one plan, one live run, and the two actions.
 *
 * **Nothing here starts anything on its own.** The hook asks what a run would do when it
 * mounts — which is safe, because the adapter that answers writes nothing, starts nothing and
 * makes no request — and it reads the live run out of the server's memory on a timer. When the
 * run retires, it follows that id into recent memory for the terminal status. Starting happens
 * from `start`, which is only ever called from a press.
 *
 * **Polling, not streaming.** A second while a run is going, ten seconds otherwise. Server-Sent
 * Events would be the "proper" answer and are the wrong first move: they add reconnection
 * semantics, a second response mode in a server that has none, and a class of "the stream
 * ended but the run did not" bugs, in exchange for saving a loopback `GET` per second against
 * an object in memory.
 *
 * **The run outlives this component.** It belongs to the API server, not to a browser tab, so
 * closing the window or moving to another route does not stop it and coming back shows it
 * again. That is the behaviour to want: a run that takes minutes should not be hostage to a
 * tab. What must not outlive anything is the child process when the *server* goes away, and
 * that is handled where it can be — `server/main.ts`.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { PlanKind, RunKind, RunPlan, RunState } from "../../shared/runs.ts";
import { planRun, readCurrentRun, readRecentRuns, RunRequestError, startRun, stopRun } from "./api.ts";

const LIVE_POLL_MS = 1_000;
const IDLE_POLL_MS = 10_000;

/** A live component and nothing else: a state setter called after unmount is a React warning. */
function useMounted(): { readonly current: boolean } {
	const mounted = useRef(true);
	useEffect(() => {
		mounted.current = true;
		return () => {
			mounted.current = false;
		};
	}, []);
	return mounted;
}

export type PlanControl = {
	/** What this kind of run would do, once the pipeline has been asked. Null while asking. */
	readonly plan: RunPlan | null;
	/** Why there is no plan. Rendered as the reason a control is off, not as a crash. */
	readonly planError: RunRequestError | null;
	/** Ask again. A plan describes this Install as it was, and a run changes it. */
	readonly refresh: () => void;
};

/**
 * Asks what a run of `kind` would do.
 *
 * **Asking is safe from a render, and starting is not, which is why this is its own hook.**
 * The adapter behind `POST /api/runs/plan` writes nothing, starts nothing and makes no
 * request, so a screen may ask on mount. This hook has no `start`; the only module
 * that starts a run is `useRunControl` below, and it starts one from a press.
 */
export function useRunPlan(kind: PlanKind, token = 0): PlanControl {
	const [plan, setPlan] = useState<RunPlan | null>(null);
	const [planError, setPlanError] = useState<RunRequestError | null>(null);
	const mounted = useMounted();

	const refresh = useCallback(() => {
		planRun(kind)
			.then((answer) => {
				if (!mounted.current) return;
				setPlan(answer.plan);
				setPlanError(null);
			})
			.catch((error: RunRequestError) => {
				if (!mounted.current) return;
				setPlan(null);
				setPlanError(error);
			});
	}, [kind, mounted, token]);

	useEffect(refresh, [refresh]);

	return { plan, planError, refresh };
}

export type RunControl = {
	/** What a run would do, once the pipeline has been asked. Null while asking, or on refusal. */
	readonly plan: RunPlan | null;
	/** Why there is no plan. Rendered as the reason a control is off, not as a crash. */
	readonly planError: RunRequestError | null;
	/** The run this Install's server is supervising, if any. */
	readonly run: RunState | null;
	/** A press is in flight; the controls are held rather than pressable twice. */
	readonly busy: boolean;
	/** A refusal from the last press. Cleared by the next one. */
	readonly actionError: RunRequestError | null;
	readonly start: () => void;
	readonly stop: () => void;
};

export function useRunControl(
	kind: RunKind,
	planToken = 0,
	onSettled?: (() => void) | undefined,
): RunControl {
	const { plan, planError, refresh: refreshPlan } = useRunPlan(kind, planToken);
	const [run, setRun] = useState<RunState | null>(null);
	const [busy, setBusy] = useState(false);
	const [actionError, setActionError] = useState<RunRequestError | null>(null);
	const mounted = useMounted();

	/**
	 * The live run, re-read on a timer whose period follows the run rather than the clock.
	 *
	 * A finished run stays on screen: `readCurrentRun` returns null once the server has
	 * retired it, and overwriting a terminal `RunState` with null would erase what happened
	 * the instant it happened. So a null answer is only taken while nothing terminal is being
	 * shown, and a new run replaces it.
	 */
	useEffect(() => {
		let cancelled = false;
		let timer: ReturnType<typeof setTimeout> | null = null;
		const tick = () => {
			const read = async () => {
				const answer = await readCurrentRun();
				if (cancelled) return;
				if (answer.run !== null) {
					setRun(answer.run);
					return;
				}
				if (run?.phase !== "running") return;
				// The server moves a settled run from `current` to `recent` in one operation.
				// Read that handoff before clearing the live state, or a fast run can disappear
				// between two polls without its terminal status reaching the screen.
				const recent = await readRecentRuns();
				if (cancelled) return;
				const finished = recent.runs.find((entry) => entry.id === run.id) ?? null;
				setRun(finished?.kind === kind ? finished : null);
			};
			read()
				.catch(() => {
					// A poll that could not be answered says nothing about the run. The next one
					// will, and inventing a failure here would put one on screen that never
					// happened.
				})
				.finally(() => {
					if (cancelled) return;
					timer = setTimeout(tick, run?.phase === "running" ? LIVE_POLL_MS : IDLE_POLL_MS);
				});
		};
		tick();
		return () => {
			cancelled = true;
			if (timer !== null) clearTimeout(timer);
		};
	}, [kind, run?.id, run?.phase]);

	/**
	 * Once a run is over, ask again what the next one would do.
	 *
	 * A plan describes this Install as it was; a run that has just appended Postings and
	 * rebuilt the view has changed what the next one would find.
	 */
	const settled = useRef<string | null>(null);
	useEffect(() => {
		if (run === null || run.phase === "running" || run.kind !== kind || settled.current === run.id) return;
		settled.current = run.id;
		refreshPlan();
		onSettled?.();
	}, [kind, onSettled, refreshPlan, run]);

	const start = useCallback(() => {
		if (plan === null || busy) return;
		setBusy(true);
		setActionError(null);
		startRun(plan.token)
			.then((answer) => {
				if (!mounted.current) return;
				setRun(answer.run);
			})
			.catch((error: RunRequestError) => {
				if (!mounted.current) return;
				// A second press while one is going is not a failure: the server hands back the
				// run that is already going, and watching it is what the person meant.
				if (error.run !== null) setRun(error.run);
				else setActionError(error);
				// A plan that has aged out is replaced rather than retried behind the person's
				// back: the next press is against numbers they were shown.
				if (error.code === "plan_expired") refreshPlan();
			})
			.finally(() => {
				if (mounted.current) setBusy(false);
			});
	}, [busy, mounted, plan, refreshPlan]);

	const stop = useCallback(() => {
		if (busy) return;
		setBusy(true);
		setActionError(null);
		stopRun()
			.then((answer) => {
				if (mounted.current) setRun(answer.run);
			})
			.catch((error: RunRequestError) => {
				if (mounted.current) setActionError(error);
			})
			.finally(() => {
				if (mounted.current) setBusy(false);
			});
	}, [busy]);

	return { plan, planError, run, busy, actionError, start, stop };
}
