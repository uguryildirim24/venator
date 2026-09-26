/**
 * The dashboard's single connection to its data.
 *
 * `API_BASE_URL` is the one place the origin is decided: the Vite dev server proxies /api
 * to the Hono process, a built bundle served by that same process resolves /api relatively,
 * and a desktop build sets VITE_API_BASE to the sidecar's loopback URL. Nothing else in the
 * app constructs a URL, so moving the backend is a one-line change.
 */

import { useCallback, useEffect, useState } from "react";

import type {
	JevTriageResponse,
	PostingDetail,
	PostingsResponse,
	SummaryResponse,
} from "../shared/contracts.ts";
import type { OnboardingStateResponse } from "../shared/onboarding.ts";

export const API_BASE_URL = import.meta.env?.VITE_API_BASE ?? "/api";

export type RequestState<Value> =
	| { readonly status: "loading" }
	| { readonly status: "error"; readonly message: string }
	| { readonly status: "ready"; readonly value: Value };

/** The local application record returned by the application routes. */
export type ApplicationManifest = {
	readonly prepared?: boolean;
	readonly version?: string | null;
	readonly provider?: string | null;
	readonly resumeText?: string | null;
	readonly letterText?: string | null;
	readonly changes?: readonly string[];
	readonly message?: string | null;
	readonly warnings?: readonly string[];
};

export type ApplicationProvider = "claude" | "codex" | "api";
export type ApplicationAction = "prepare" | "save" | "dismiss" | "restore" | "applied" | "handoff";

export type ApplicationActionResult = ApplicationManifest & {
	readonly message?: string | null;
	readonly warnings?: readonly string[];
};

type ApplicationActionBody = {
	action: ApplicationAction;
	key: string;
	provider?: ApplicationProvider;
	coverLetter?: boolean;
};

type ApiFailure = {
	readonly error?: string;
};

async function readJson<Value>(path: string, signal: AbortSignal): Promise<Value> {
	const response = await fetch(`${API_BASE_URL}${path}`, { signal, headers: { accept: "application/json" } });
	if (!response.ok) {
		const failure: ApiFailure = await response.json().catch(() => ({}));
		throw new Error(failure.error ?? `${response.status} ${response.statusText}`);
	}
	// The server is this repo's own read-only API and shares shared/contracts.ts with the
	// app, so the response is already the parsed domain type rather than foreign input.
	return await response.json();
}

/** Reads the application manifest for the selected Posting. */
export function useApplication(key: string | null, reloadToken: number): RequestState<ApplicationManifest> {
	return useApi<ApplicationManifest>(key === null ? null : `/applications/${encodeURIComponent(key)}`, reloadToken);
}

/** The document endpoint is a link so the browser can preview or download it directly. */
export function applicationFileUrl(
	key: string,
	name: "resume.pdf" | "resume.txt" | "letter.txt",
): string {
	return `${API_BASE_URL}/applications/${encodeURIComponent(key)}/files/${name}`;
}

/** Sends one explicit local application action. No action is started while rendering. */
export async function applicationAction(
	key: string,
	action: ApplicationAction,
	options: { readonly provider?: ApplicationProvider; readonly coverLetter?: boolean } = {},
): Promise<ApplicationActionResult> {
	const body: ApplicationActionBody = { action, key };
	if (options.provider !== undefined) body.provider = options.provider;
	if (options.coverLetter === true) body.coverLetter = true;
	const response = await fetch(`${API_BASE_URL}/applications`, {
		method: "POST",
		headers: {
			accept: "application/json",
			"content-type": "application/json",
			"X-Venator-Application": "1",
		},
		body: JSON.stringify(body),
	});
	if (!response.ok) {
		const failure: ApiFailure = await response.json().catch(() => ({}));
		throw new Error(failure.error ?? `${response.status} ${response.statusText}`);
	}
	const result: ApplicationActionResult = await response.json();
	return result;
}

/**
 * Loads `path`, re-running whenever it changes or `reloadToken` advances. The token is how
 * the app refreshes after the pipeline rebuilds build/venator.db underneath it. A null path
 * asks for nothing and stays loading: a screen that reads one of two lists calls both hooks
 * and passes null to the one it is not showing.
 */
export function useApi<Value>(path: string | null, reloadToken: number): RequestState<Value> {
	const [response, setResponse] = useState<{
		readonly path: string | null;
		readonly reloadToken: number;
		readonly state: RequestState<Value>;
	}>({ path, reloadToken, state: { status: "loading" } });

	useEffect(() => {
		if (path === null) return;
		const controller = new AbortController();
		setResponse({ path, reloadToken, state: { status: "loading" } });
		readJson<Value>(path, controller.signal)
			.then((value) => {
				if (!controller.signal.aborted) setResponse({ path, reloadToken, state: { status: "ready", value } });
			})
			.catch((error: Error) => {
				if (controller.signal.aborted) return;
				setResponse({ path, reloadToken, state: { status: "error", message: error.message } });
			});
		return () => controller.abort();
	}, [path, reloadToken]);

	// Never render the previous page under the new page's count or navigation links.
	return path !== null && response.path === path && response.reloadToken === reloadToken ? response.state : { status: "loading" };
}

export type LocationSelection = {
	readonly selected: readonly string[];
	readonly choices: readonly { readonly key: string; readonly label: string; readonly count: number; readonly parent?: string }[];
};

export function useLocations(reloadToken: number): RequestState<LocationSelection> {
	return useApi<LocationSelection>("/locations", reloadToken);
}

export async function saveLocations(selected: readonly string[]): Promise<void> {
	const response = await fetch(`${API_BASE_URL}/locations`, {
		method: "POST", headers: { "content-type": "application/json", "X-Venator-Location": "1" }, body: JSON.stringify({ selected }),
	});
	if (!response.ok) throw new Error("The location choice could not be saved.");
}

export function useSummary(reloadToken: number): RequestState<SummaryResponse> {
	return useApi<SummaryResponse>("/summary", reloadToken);
}

/**
 * Whether this Install has a Profile yet — the question the app answers before it decides
 * between onboarding and the dashboard.
 *
 * A read: it lists directories and opens the view database once, and it is the only part of
 * onboarding that is safe to call on load. Everything else on that surface writes or spends
 * something and is called from a handler for something the Owner pressed
 * (`src/onboarding/api.ts`).
 */
export function useOnboardingState(reloadToken: number): RequestState<OnboardingStateResponse> {
	return useApi<OnboardingStateResponse>("/onboarding/state", reloadToken);
}

export function usePostings(query: string | null, reloadToken: number): RequestState<PostingsResponse> {
	return useApi<PostingsResponse>(query === null ? null : `/postings${query}`, reloadToken);
}

export function usePostingDetail(key: string | null, reloadToken: number): RequestState<PostingDetail> {
	return useApi<PostingDetail>(key === null ? null : `/postings/${encodeURIComponent(key)}`, reloadToken);
}

export function useJevTriage(query: string | null, reloadToken: number): RequestState<JevTriageResponse> {
	return useApi<JevTriageResponse>(query === null ? null : `/jev-triage${query}`, reloadToken);
}

/** A counter the whole app shares so one keystroke refetches every open view. */
export function useReloadToken(): readonly [number, () => void] {
	const [token, setToken] = useState(0);
	const reload = useCallback(() => setToken((previous) => previous + 1), []);
	return [token, reload];
}
