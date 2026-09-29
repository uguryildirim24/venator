/**
 * One Posting's application, as the toolbar and the margin both act on it.
 *
 * The toolbar saves, dismisses, restores and applies; the margin reviews
 * documents and records that the Owner applied. They are one control split across the window,
 * so they share one busy flag and one answer: a second press is held while the first is in
 * flight, whichever half it came from. Nothing here runs on render.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { PostingDetail } from "../shared/contracts.ts";
import {
	applicationAction,
	useApplication,
	type ApplicationAction,
	type ApplicationManifest,
	type RequestState,
} from "./api.ts";

export type ApplicationControl = {
	readonly manifest: RequestState<ApplicationManifest>;
	/** Documents are prepared and verified for Fill. */
	readonly prepared: boolean;
	readonly busy: ApplicationAction | null;
	readonly error: string | null;
	readonly failedAction: ApplicationAction | null;
	/** Successful document version to open after the refreshed manifest arrives. */
	readonly reviewVersion: string | null;
	readonly notice: string | null;
	readonly warnings: readonly string[];
	readonly run: (action: ApplicationAction, options?: { readonly version?: string; readonly edits?: readonly { readonly draft_id: string; readonly text: string }[] }) => void;
};

// Whether this Posting can be rechecked by Apply.
export type Preparation = {
	readonly allowed: boolean;
};

// Sources whose exact listing Apply can recheck before drafting.
const REFRESHABLE_SOURCES = new Set(["greenhouse", "lever", "ashby", "workday"]);

/**
 * Apply rechecks the exact listing first, so it needs a source it can recheck, a listing
 * not known to be closed, and no recorded hard conflict.
 */
export function preparation(detail: PostingDetail): Preparation {
	const assessment = detail.assessment;
	const refreshable = REFRESHABLE_SOURCES.has(detail.posting.source.trim().toLowerCase().split(":", 1)[0] ?? "");
	if (!refreshable) {
		return { allowed: false };
	}
	if (assessment?.listingStatus === "closed" || (assessment?.conflicts.length ?? 0) > 0) return { allowed: false };
	return { allowed: true };
}

export function useApplicationControl(key: string | null, reloadToken: number, onReload: () => void): ApplicationControl {
	const manifest = useApplication(key, reloadToken);
	const appliedKey = useRef<string | null>(null);
	useEffect(() => {
		if (key !== null && manifest.status === "ready" && manifest.value.applied && appliedKey.current !== key) {
			appliedKey.current = key;
			onReload();
		}
	}, [key, manifest, onReload]);
	const [busy, setBusy] = useState<ApplicationAction | null>(null);
	const [answer, setAnswer] = useState<{
		readonly key: string;
		readonly error: string | null;
		readonly failedAction: ApplicationAction | null;
		readonly reviewVersion: string | null;
		readonly notice: string | null;
		readonly warnings: readonly string[];
	} | null>(null);

	const run = useCallback<ApplicationControl["run"]>(
		(action, options = {}) => {
			if (busy !== null || key === null) return;
			setBusy(action);
			setAnswer(null);
			applicationAction(key, action, options)
				.then((result) => {
					const message = action === "apply" || action === "edit" ? "" : result.message?.trim() ?? "";
					setAnswer({ key, error: null, failedAction: null,
						reviewVersion: action === "apply" || action === "edit" ? result.version ?? null : null,
						notice: message === "" ? null : message, warnings: result.warnings ?? [] });
					onReload();
				})
				.catch((reason: Error) => setAnswer({ key, error: reason.message, failedAction: action, reviewVersion: null, notice: null, warnings: [] }))
				.finally(() => setBusy(null));
		},
		[busy, key, onReload],
	);

	// An answer belongs to the Posting it was about; moving to the next one clears it.
	const current = answer?.key === key ? answer : null;
	return {
		manifest,
		prepared: manifest.status === "ready" && manifest.value.prepared === true,
		busy,
		error: current?.error ?? null,
		failedAction: current?.failedAction ?? null,
		reviewVersion: current?.reviewVersion ?? null,
		notice: current?.notice ?? null,
		warnings: current?.warnings ?? [],
		run,
	};
}
