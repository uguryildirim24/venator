import { Bookmark, EyeOff, PanelLeft, Search, SquareArrowOutUpRight } from "lucide-react";
import { useEffect, useState, type ReactNode, type RefObject } from "react";

import type { PostingDetail } from "../../shared/contracts.ts";
import type { ApplicationControl } from "../application.ts";
import { reservesTrafficLights } from "../platform.ts";
import { ExternalLink } from "./external-link.tsx";

export type SearchProps = {
	readonly value: string;
	readonly placeholder: string;
	readonly onCommit: (search: string) => void;
	readonly field: RefObject<HTMLInputElement | null>;
};

/** The toolbar's search. It commits after a pause in typing, so every keystroke is not a route. */
export function SearchField({ value, placeholder, onCommit, field }: SearchProps) {
	const [draft, setDraft] = useState(value);
	useEffect(() => setDraft(value), [value]);
	useEffect(() => {
		if (draft === value) return;
		const timer = setTimeout(() => onCommit(draft), 180);
		return () => clearTimeout(timer);
	}, [draft, value, onCommit]);
	return (
		<label className="search">
			<Search aria-hidden="true" className="icon" />
			<input ref={field} type="search" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={placeholder} aria-label={placeholder} />
		</label>
	);
}

type ToolbarProps = {
	readonly title: string;
	readonly subtitle: string | null;
	readonly search: SearchProps | null;
	/** The sidebar is hidden, so its toggle moves here. */
	readonly sidebarHidden: boolean;
	readonly onShowSidebar: () => void;
	/** The Posting's actions, over the detail column. A single-column route leaves this out. */
	readonly actions?: ReactNode;
	readonly layout?: "single" | undefined;
};

/**
 * The unified toolbar: no fill of its own, the list's name and count over the list, search at
 * the list column's right edge, and the Posting's actions over the Posting. No logo, no
 * wordmark: the menu bar and the Dock name the app.
 */
export function Toolbar({ title, subtitle, search, sidebarHidden, onShowSidebar, actions, layout }: ToolbarProps) {
	return (
		<header className="toolbar" data-layout={layout} data-tauri-drag-region>
			<div className="toolbar-list" data-lights={sidebarHidden && reservesTrafficLights ? "reserved" : undefined} data-tauri-drag-region>
				{sidebarHidden ? (
					<button type="button" className="icon-button" onClick={onShowSidebar} aria-label="Show the sidebar" title="Show the sidebar">
						<PanelLeft aria-hidden="true" className="icon" />
					</button>
				) : null}
				<div className="toolbar-heading" data-tauri-drag-region>
					<p className="toolbar-title">{title}</p>
					{subtitle === null ? null : <span className="toolbar-subtitle">{subtitle}</span>}
				</div>
				{search === null ? null : <SearchField {...search} />}
			</div>
			<div className="toolbar-actions" data-tauri-drag-region>
				{actions}
			</div>
		</header>
	);
}

type PostingActionsProps = {
	readonly detail: PostingDetail;
	readonly control: ApplicationControl;
};

/**
 * Open in the browser, save, dismiss, and the one prominent button: Open Application.
 *
 * Open Application is the visible handoff. It opens the employer's form in a browser the
 * Owner can see, fills confirmed fields, uploads the prepared documents, and stops; the Owner
 * presses submit. It needs prepared documents, and the margin says so beside a Posting that
 * has none. An excluded Posting has no Owner action and no capsule; a dismissed one has
 * Restore in its place.
 */
export function PostingActions({ detail, control }: PostingActionsProps) {
	const state = detail.application?.state ?? null;
	const excluded = detail.status === "hard-killed";
	const busy = control.busy !== null;
	// Saving is the first step of an application; once there is a later one, it has happened.
	const savable = state === null || state === "queued";
	return (
		<>
			<div className="icon-group">
				<ExternalLink className="icon-link" href={detail.posting.url} title="Open in your browser">
					<SquareArrowOutUpRight aria-hidden="true" className="icon" />
					<span className="visually-hidden">Open in your browser</span>
				</ExternalLink>
				{excluded ? null : (
					<>
						<button
							type="button"
							aria-pressed={state === "approved"}
							disabled={busy || !savable}
							onClick={() => control.run("save")}
							aria-label={state === "approved" ? "Saved" : "Save"}
							title={state === "approved" ? "Saved" : "Save"}
						>
							<Bookmark aria-hidden="true" className="icon" />
						</button>
						<button type="button" disabled={busy || state === "rejected"} onClick={() => control.run("dismiss")} aria-label="Dismiss" title="Dismiss">
							<EyeOff aria-hidden="true" className="icon" />
						</button>
					</>
				)}
			</div>
			{excluded ? null : state === "rejected" ? (
				<button type="button" className="capsule" data-tone="plain" disabled={busy} onClick={() => control.run("restore")}>
					{control.busy === "restore" ? "Restoring…" : "Restore"}
				</button>
			) : (
				<button type="button" className="capsule" disabled={busy || !control.prepared} onClick={() => control.run("handoff")}>
					{control.busy === "handoff" ? "Opening…" : "Open Application"}
				</button>
			)}
		</>
	);
}
