/**
 * One Posting's application, as the toolbar and the margin both act on it.
 *
 * The toolbar saves, dismisses, restores and opens the application; the margin prepares
 * documents and records that the Owner applied. They are one control split across the window,
 * so they share one busy flag and one answer: a second press is held while the first is in
 * flight, whichever half it came from. Nothing here runs on render.
 */

import { useCallback, useState } from "react";

import type { PostingDetail } from "../shared/contracts.ts";
import {
	applicationAction,
	useApplication,
	type ApplicationAction,
	type ApplicationManifest,
	type ApplicationProvider,
	type RequestState,
} from "./api.ts";

export type ApplicationControl = {
	readonly manifest: RequestState<ApplicationManifest>;
	/** Documents are prepared and verified: Open Application can hand them off. */
	readonly prepared: boolean;
	readonly busy: ApplicationAction | null;
	readonly error: string | null;
	readonly notice: string | null;
	readonly warnings: readonly string[];
	readonly run: (action: ApplicationAction, options?: { readonly provider?: ApplicationProvider; readonly coverLetter?: boolean }) => void;
};

/** Whether documents can be prepared, and the sentence that says so or says why not. */
export type Preparation = {
	readonly allowed: boolean;
	readonly note: string;
};

/** Sources whose exact listing Prepare can recheck before it writes anything. */
const REFRESHABLE_SOURCES = new Set(["greenhouse", "lever", "ashby", "workday"]);

/**
 * Whether documents can be prepared for this Posting, and the sentence that says why not.
 *
 * Preparation rechecks the exact listing first, so it needs a source it can recheck, a listing
 * not known to be closed, and no recorded hard conflict.
 */
export function preparation(detail: PostingDetail): Preparation {
	const assessment = detail.assessment;
	const refreshable = REFRESHABLE_SOURCES.has(detail.posting.source.trim().toLowerCase().split(":", 1)[0] ?? "");
	if (!refreshable) {
		return { allowed: false, note: "This source cannot be rechecked automatically. Open the employer's listing and apply there." };
	}
	if (assessment?.listingStatus === "closed") return { allowed: false, note: "This listing is closed, so nothing can be prepared for it." };
	if ((assessment?.conflicts.length ?? 0) > 0) return { allowed: false, note: "Preparation waits while a hard conflict is recorded." };
	const needsCheck = assessment === undefined || assessment.listingStatus !== "open" || assessment.descriptionKind !== "full";
	return {
		allowed: true,
		note: needsCheck
			? "Checks the exact listing first, then makes a tailored résumé from your confirmed facts."
			: "Makes a tailored résumé from your confirmed facts, after rechecking the listing.",
	};
}

export function useApplicationControl(key: string | null, reloadToken: number, onReload: () => void): ApplicationControl {
	const manifest = useApplication(key, reloadToken);
	const [busy, setBusy] = useState<ApplicationAction | null>(null);
	const [answer, setAnswer] = useState<{
		readonly key: string;
		readonly error: string | null;
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
					const message = result.message?.trim() ?? "";
					setAnswer({ key, error: null, notice: message === "" ? null : message, warnings: result.warnings ?? [] });
					onReload();
				})
				.catch((reason: Error) => setAnswer({ key, error: reason.message, notice: null, warnings: [] }))
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
		notice: current?.notice ?? null,
		warnings: current?.warnings ?? [],
		run,
	};
}
