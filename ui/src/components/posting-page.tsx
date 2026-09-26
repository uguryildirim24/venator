import { Check, ChevronRight, Loader, Lock } from "lucide-react";
import { Fragment, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { FilterDecision, PostingDetail, TrackEvent } from "../../shared/contracts.ts";
import { applicationFileUrl, type ApplicationManifest, type ApplicationProvider } from "../api.ts";
import { preparation, type ApplicationControl } from "../application.ts";
import { formatDay, formatMoment, hostOf } from "../format.ts";
import { assessmentNoteLabel, decisionTitle, employerLabel, ruleLabel, sourceLabel, statusLabel, trackEventLabel } from "../labels.ts";
import { buildMargin, segments, type MarginNote, type MarkedBlock } from "../margin.ts";
import { ExternalLink } from "./external-link.tsx";
import { Sheet } from "./sheet.tsx";

/* ----------------------------------------------------------------- notes */

function Note({ note, onOpen }: { readonly note: MarginNote; readonly onOpen: () => void }) {
	return (
		<div className="note" data-tone={note.tone === "neutral" ? undefined : note.tone}>
			<span className="note-title">{note.title}</span>
			{note.subtitle === null ? null : <span className="note-subtitle">{note.subtitle}</span>}
			{note.detail === null || note.detail === undefined ? null : <span className="note-subtitle">{note.detail}</span>}
			{note.link === undefined ? null : (
				<a className="note-action" href={note.link.hash}>
					{note.link.label}
				</a>
			)}
			{note.opens === undefined ? null : (
				<button type="button" className="note-action" onClick={onOpen}>
					Show them
				</button>
			)}
		</div>
	);
}

function Notes({ notes, count, onOpen }: { readonly notes: readonly MarginNote[]; readonly count?: string | null; readonly onOpen: () => void }) {
	if (notes.length === 0 && (count === null || count === undefined)) return null;
	return (
		<div className="margin-stack">
			{count === null || count === undefined ? null : <span className="margin-count">{count}</span>}
			{notes.map((note) => (
				<Note key={note.key} note={note} onOpen={onOpen} />
			))}
		</div>
	);
}

/* ----------------------------------------------------------------- page */

function BlockText({ block }: { readonly block: MarkedBlock }) {
	return (
		<>
			{segments(block).map((run, index) =>
				run.tone === null ? (
					<Fragment key={index}>{run.text}</Fragment>
				) : (
					<span key={index} className="mark" data-mark={run.tone}>
						{run.text}
					</span>
				),
			)}
		</>
	);
}

function Block({ block, onOpen }: { readonly block: MarkedBlock; readonly onOpen: () => void }) {
	const text =
		block.kind === "heading" ? (
			<h2 className="page-heading">
				<BlockText block={block} />
			</h2>
		) : (
			<p className={block.kind === "item" ? "page-text page-item" : "page-text"}>
				<BlockText block={block} />
			</p>
		);
	return (
		<div className="page-block" data-kind={block.kind} role={block.kind === "item" ? "listitem" : undefined}>
			<div className="page-cell">{text}</div>
			<div className="margin-cell">
				<Notes notes={block.notes} count={block.count} onOpen={onOpen} />
			</div>
		</div>
	);
}

/** Consecutive list items, as one list. The list and its items lay out as the grid's own rows. */
function PageBlocks({ blocks, onOpen }: { readonly blocks: readonly MarkedBlock[]; readonly onOpen: () => void }) {
	const runs: MarkedBlock[][] = [];
	for (const block of blocks) {
		const last = runs.at(-1);
		if (block.kind === "item" && last?.[0]?.kind === "item") last.push(block);
		else runs.push([block]);
	}
	return (
		<>
			{runs.map((run, index) =>
				run[0]?.kind === "item" ? (
					<div key={index} className="page-list" role="list">
						{run.map((block, item) => (
							<Block key={item} block={block} onOpen={onOpen} />
						))}
					</div>
				) : (
					run.map((block) => <Block key={index} block={block} onOpen={onOpen} />)
				),
			)}
		</>
	);
}

/** Why there is no page to read, in words. */
function emptyPageReason(detail: PostingDetail): string {
	const kind = detail.assessment?.descriptionKind;
	if (kind === "snippet") return "Only a snippet of the description was fetched, and it has no text to set here.";
	if (detail.descriptionHtml.trim() !== "") return "The description has no text a reader page can hold. Read Full Description shows it as the employer wrote it.";
	return "The full description has not been fetched yet. It arrives with the next detail check of this listing.";
}

/* ----------------------------------------------------------------- the employer's page */

/**
 * The employer's HTML, in an opaque frame: `sandbox=""` runs no script and reaches no origin,
 * and the frame's own policy loads nothing but inline style and inline images. Its colours and
 * type are read from the token layer as it stands when the sheet opens, so the frame follows
 * the scheme without a value of its own.
 */
function frameDocument(html: string): string {
	const style = getComputedStyle(document.documentElement);
	const token = (name: string) => style.getPropertyValue(name).trim();
	const scheme = style.colorScheme === "" ? "light" : style.colorScheme;
	const css =
		`:root{color-scheme:${scheme}}` +
		`body{margin:0;padding:${token("--space-4")};background:transparent;color:${token("--label-primary")};` +
		`font-family:${token("--font-page")};font-size:${token("--size-page-text")};line-height:${token("--leading-page-text")}}` +
		"a{color:inherit}img{max-width:100%}";
	return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:"><style>${css}</style></head><body>${html}</body></html>`;
}

/* ----------------------------------------------------------------- history sheets */

function DecisionNote({ decision }: { readonly decision: FilterDecision }) {
	return (
		<div className="note">
			<span className="note-title">{decisionTitle(decision.stage, decision.verdict)}</span>
			{decision.rule === null ? null : <span className="note-subtitle">{ruleLabel(decision.rule)}</span>}
			<span className="note-subtitle">{decision.reason === null ? "No reason was recorded, which the pipeline should never do." : assessmentNoteLabel(decision.reason)}</span>
			<span className="note-subtitle" title={`Filters ${decision.filtersVersion ?? "unrecorded"}`}>
				{formatMoment(decision.decidedAt)}
				{decision.latest ? "" : " · Superseded by a later decision"}
			</span>
		</div>
	);
}

function TrackNote({ event }: { readonly event: TrackEvent }) {
	return (
		<div className="note">
			<span className="note-title">{trackEventLabel(event.event)}</span>
			<span className="note-subtitle">{formatMoment(event.at)}</span>
			{event.detail === null || event.detail.trim() === "" ? null : <span className="note-subtitle">{event.detail}</span>}
		</div>
	);
}

function FootLink({ label, count, noun, onPress }: { readonly label: string; readonly count: number; readonly noun: readonly [string, string]; readonly onPress: () => void }) {
	return (
		<button type="button" className="link-button" onClick={onPress}>
			{label}{" "}
			<span className="count">
				{count} {count === 1 ? noun[0] : noun[1]}
			</span>
			<ChevronRight aria-hidden="true" className="chevron" />
		</button>
	);
}

/* ----------------------------------------------------------------- the application */

const PROVIDERS: readonly { readonly value: ApplicationProvider; readonly label: string; readonly help: string }[] = [
	{ value: "claude", label: "Claude", help: "Uses the installed Claude CLI with its local sign-in." },
	{ value: "codex", label: "ChatGPT via Codex", help: "Uses the installed Codex CLI with its local sign-in." },
	{ value: "api", label: "Configured API", help: "Uses the API configured on this machine." },
];

function ResumeThumb() {
	return (
		<span className="resume-thumb" aria-hidden="true">
			<span />
			<span />
			<span />
			<span />
			<span />
			<span />
			<span />
		</span>
	);
}

function QuickLook({ postingKey, manifest, open, onOpenChange }: { readonly postingKey: string; readonly manifest: ApplicationManifest; readonly open: boolean; readonly onOpenChange: (open: boolean) => void }) {
	const [view, setView] = useState<"pdf" | "text" | "letter">("pdf");
	const pdf = applicationFileUrl(postingKey, "resume.pdf");
	return (
		<Sheet open={open} onOpenChange={onOpenChange} title="Tailored résumé" description="The prepared documents, as they will be uploaded." size="wide">
			<div className="segmented" role="group" aria-label="Document">
				<button type="button" aria-pressed={view === "pdf"} onClick={() => setView("pdf")}>
					Résumé
				</button>
				<button type="button" aria-pressed={view === "text"} onClick={() => setView("text")}>
					Résumé text
				</button>
				{manifest.letterText ? (
					<button type="button" aria-pressed={view === "letter"} onClick={() => setView("letter")}>
						Cover letter
					</button>
				) : null}
			</div>
			{view === "pdf" ? <iframe key={manifest.version} src={pdf} title="Prepared résumé" className="sheet-frame" /> : null}
			{view === "text" ? <pre className="sheet-text">{manifest.resumeText ?? "The résumé text is not available."}</pre> : null}
			{view === "letter" ? <pre className="sheet-text">{manifest.letterText}</pre> : null}
			{manifest.changes?.length ? (
				<div className="sheet-notes">
					<span className="margin-count">What was tailored</span>
					{manifest.changes.map((change) => (
						<div key={change} className="note">
							<span className="note-subtitle">{change}</span>
						</div>
					))}
				</div>
			) : null}
			<div className="empty-actions">
				<a className="button" href={pdf} download>
					Download the PDF
				</a>
			</div>
		</Sheet>
	);
}

function PrepareSheet({
	detail,
	control,
	open,
	onOpenChange,
}: {
	readonly detail: PostingDetail;
	readonly control: ApplicationControl;
	readonly open: boolean;
	readonly onOpenChange: (open: boolean) => void;
}) {
	const previous = control.manifest.status === "ready" ? control.manifest.value.provider : null;
	const [provider, setProvider] = useState<ApplicationProvider>(PROVIDERS.find((entry) => entry.value === previous)?.value ?? "claude");
	const [coverLetter, setCoverLetter] = useState(false);
	const { allowed, note } = preparation(detail);
	return (
		<Sheet open={open} onOpenChange={onOpenChange} title="Prepare Application" description={note}>
			<div className="form-group">
				<label className="form-row">
					<span>Prepare with</span>
					<select value={provider} onChange={(event) => setProvider(PROVIDERS.find((entry) => entry.value === event.target.value)?.value ?? "claude")}>
						{PROVIDERS.map((entry) => (
							<option key={entry.value} value={entry.value}>
								{entry.label}
							</option>
						))}
					</select>
				</label>
				<label className="form-row form-check">
					<input type="checkbox" checked={coverLetter} onChange={(event) => setCoverLetter(event.target.checked)} />
					<span>Include a cover letter</span>
				</label>
			</div>
			<p className="form-help">
				{PROVIDERS.find((entry) => entry.value === provider)?.help} Preparing is one completion on that runtime. Nothing is sent to the employer.
			</p>
			<div className="sheet-actions">
				<button
					type="button"
					className="button"
					// Opening this sheet costs nothing; the press waits until the documents on
					// record have been read, so it never prepares over ones it has not seen.
					disabled={!allowed || control.busy !== null || control.manifest.status === "loading"}
					onClick={() => {
						control.run("prepare", { provider, coverLetter });
						onOpenChange(false);
					}}
				>
					Prepare
				</button>
			</div>
		</Sheet>
	);
}

function ApplicationMargin({ detail, control, onHistory }: { readonly detail: PostingDetail; readonly control: ApplicationControl; readonly onHistory: () => void }) {
	const [quickLook, setQuickLook] = useState(false);
	const [prepareOpen, setPrepareOpen] = useState(false);
	const state = detail.application?.state ?? null;
	const since = detail.application?.since ?? null;
	const { allowed, note } = preparation(detail);
	const manifest = control.manifest.status === "ready" ? control.manifest.value : null;

	let body: ReactNode;
	if (control.busy === "prepare") {
		body = (
			<div className="resume-state">
				<Loader aria-hidden="true" className="glyph spinner" />
				<span>Preparing…</span>
			</div>
		);
	} else if (state === "submitted" || state === "concluded") {
		body = (
			<div className="resume-text">
				<span className="resume-state">
					<Check aria-hidden="true" className="glyph check" />
					<span>
						{state === "concluded" ? "Employer responded" : "Applied"} {formatDay(since)}
					</span>
				</span>
				<button type="button" className="note-action" onClick={onHistory}>
					History
				</button>
			</div>
		);
	} else if (state === "rejected") {
		body = (
			<div className="note">
				<span className="note-title">Dismissed</span>
				<span className="note-subtitle">On {formatDay(since)}. Restore puts it back in its list.</span>
			</div>
		);
	} else if (state === "withdrawn" || state === "filled") {
		body = (
			<div className="note">
				<span className="note-title">{state === "withdrawn" ? "Withdrawn" : "Form filled in a Dry Run"}</span>
				<span className="note-subtitle">On {formatDay(since)}</span>
			</div>
		);
	} else if (control.prepared && manifest !== null) {
		body = (
			<>
				<div className="resume">
					<ResumeThumb />
					<div className="resume-text">
						<strong>Tailored résumé</strong>
						<span className="resume-state">
							<Check aria-hidden="true" className="glyph check" />
							<span>Ready{since === null ? "" : ` · ${formatDay(since)}`}</span>
						</span>
					</div>
				</div>
				<div className="margin-buttons">
					<button type="button" className="button" onClick={() => setQuickLook(true)}>
						Quick Look
					</button>
					<button type="button" className="button" disabled={control.busy !== null} onClick={() => control.run("applied")}>
						{control.busy === "applied" ? "Saving…" : "I Applied"}
					</button>
				</div>
				<button type="button" className="note-action" onClick={() => setPrepareOpen(true)} disabled={!allowed}>
					Prepare again…
				</button>
				<QuickLook postingKey={detail.posting.key} manifest={manifest} open={quickLook} onOpenChange={setQuickLook} />
			</>
		);
	} else {
		body = (
			<>
				{state === "approved" ? (
					<div className="note">
						<span className="note-title">Saved</span>
						<span className="note-subtitle">On {formatDay(since)}</span>
					</div>
				) : null}
				<div className="margin-buttons">
					<button type="button" className="button" disabled={!allowed || control.busy !== null} onClick={() => setPrepareOpen(true)}>
						Prepare Application…
					</button>
				</div>
				{!allowed ? <div className="note"><span className="note-subtitle">{note}</span></div> : null}
			</>
		);
	}

	return (
		<div className="margin-application">
			{body}
			{control.manifest.status === "error" ? (
				<div className="note">
					<span className="note-title">Documents could not be read</span>
					<span className="note-subtitle">{control.manifest.message}</span>
				</div>
			) : null}
			{control.error === null ? null : (
				<div className="note" role="alert">
					<span className="note-title">That did not go through</span>
					<span className="note-subtitle">{control.error}</span>
				</div>
			)}
			{control.notice === null ? null : (
				<p className="note-subtitle" role="status">
					{control.notice}
				</p>
			)}
			{control.warnings.map((warning) => (
				<div key={warning} className="note">
					<span className="note-subtitle">{warning}</span>
				</div>
			))}
			<PrepareSheet key={detail.posting.key} detail={detail} control={control} open={prepareOpen} onOpenChange={setPrepareOpen} />
		</div>
	);
}

/* ----------------------------------------------------------------- the page */

type PostingPageProps = {
	readonly detail: PostingDetail;
	readonly control: ApplicationControl;
	/** Early review leads the margin with Jev's review flags. */
};

/**
 * The Posting as a reading page, with Venator's notes level with the lines they are about.
 *
 * Every block sits in its own row of one grid, its notes in column two of that row, so a note
 * lines up with its text without anything being measured. The page column holds only the
 * employer's words, as plain text from the reader extraction; the margin holds only Venator's.
 */
export function PostingPage({ detail, control }: PostingPageProps) {
	const [sheet, setSheet] = useState<"description" | "changed" | "history" | "requirements" | null>(null);
	const pageRef = useRef<HTMLElement>(null);

	// Keep the margin a single flowing column without letting it change the employer's
	// paragraph heights. Reflow when either column changes size (including loaded documents).
	useLayoutEffect(() => {
		const page = pageRef.current;
		if (page === null) return;
		let frame = 0;
		const layout = () => {
			const cells = [...page.querySelectorAll<HTMLElement>(".page-block > .margin-cell")];
			if ((page.parentElement?.clientWidth ?? 0) < 680) {
				for (const cell of cells) cell.style.translate = "";
				page.style.minHeight = "";
				return;
			}
			let bottom = 0;
			for (const cell of cells) {
				if (!cell.textContent?.trim()) continue;
				const anchor = cell.offsetTop;
				const top = Math.max(anchor, bottom === 0 ? anchor : bottom + 14);
				cell.style.translate = `0 ${top - anchor}px`;
				bottom = top + cell.scrollHeight;
			}
			page.style.minHeight = `${bottom + 24}px`;
		};
		const schedule = () => {
			cancelAnimationFrame(frame);
			frame = requestAnimationFrame(layout);
		};
		const observer = new ResizeObserver(schedule);
		observer.observe(page.parentElement ?? page);
		for (const cell of page.querySelectorAll<HTMLElement>(".page-block > .page-cell, .page-block > .margin-cell > *")) observer.observe(cell);
		schedule();
		return () => { cancelAnimationFrame(frame); observer.disconnect(); };
	}, [detail, control.manifest.status, control.prepared]);
	const { posting, assessment } = detail;
	const margin = useMemo(
		() =>
			buildMargin({
				page: detail.page,
				decisions: detail.decisions,
				assessment,
				jevTriage: detail.jevTriage,
			}),
		[detail, assessment],
	);
	const excluded = detail.status === "hard-killed";
	const employer = employerLabel(posting) ?? `via ${sourceLabel(posting.source)}`;
	const eyebrow = posting.location === null || posting.location === "" ? employer : `${employer} · ${posting.location}`;
	const host = hostOf(assessment?.applyUrl || posting.url);
	const meta = [
		posting.postedAt === null ? null : `Posted ${formatDay(posting.postedAt)}`,
		assessment?.listingStatus === "open" && assessment.lastVerifiedAt !== null
			? `Verified open ${formatDay(assessment.lastVerifiedAt)}`
			: assessment?.listingStatus === "closed"
				? "Closed"
				: "Not verified open",
		["no-text", "too-long", "protected", "not-filtered"].includes(detail.status) ? statusLabel(detail.status) : null,
	].filter((part) => part !== null);
	const openRequirements = () => setSheet("requirements");
	const close = (open: boolean) => {
		if (!open) setSheet(null);
	};

	return (
		<article ref={pageRef} className="posting" aria-label={posting.title}>
			<div className="page-block" data-kind="header">
				<header className="page-cell">
					<p className="page-eyebrow">{eyebrow}</p>
					<h1 className="page-title">{posting.title}</h1>
					<p className="page-meta">
						{host === "" ? null : (
							<>
								<Lock aria-hidden="true" className="glyph" />
								<span>{host}</span>
							</>
						)}
						{meta.map((part, index) => (
							<Fragment key={part}>
								{index === 0 && host === "" ? null : (
									<span className="dot" aria-hidden="true">
										·
									</span>
								)}
								<span>{part}</span>
							</Fragment>
						))}
					</p>
				</header>
				<div className="margin-cell">
					<div className="margin-stack">
						{excluded ? null : <ApplicationMargin detail={detail} control={control} onHistory={() => setSheet("history")} />}
						{margin.header.map((note) => (
							<Note key={note.key} note={note} onOpen={openRequirements} />
						))}
					</div>
				</div>
			</div>

			{margin.blocks.length === 0 ? (
				<div className="page-block" data-kind="paragraph">
					<div className="page-cell">
						<p className="page-empty">{emptyPageReason(detail)}</p>
					</div>
					<div className="margin-cell" />
				</div>
			) : (
				<PageBlocks blocks={margin.blocks} onOpen={openRequirements} />
			)}

			<div className="page-block" data-kind="foot">
				<div className="page-cell page-foot">
					{detail.descriptionHtml.trim() === "" ? (
						<>
							<ExternalLink className="button" href={posting.url} title={posting.url}>
								Open the employer's listing
							</ExternalLink>
							<span className="note-text">Opens in your browser</span>
						</>
					) : (
						<>
							<button type="button" className="button" onClick={() => setSheet("description")}>
								Read Full Description
							</button>
							<span className="note-text">Opens the employer's page, sandboxed</span>
						</>
					)}
				</div>
				<div className="margin-cell">
					<div className="margin-foot">
						{detail.decisions.length === 0 ? null : (
							<FootLink label="What changed" count={detail.decisions.length} noun={["decision", "decisions"]} onPress={() => setSheet("changed")} />
						)}
						{detail.trackEvents.length === 0 ? null : (
							<FootLink label="History" count={detail.trackEvents.length} noun={["event", "events"]} onPress={() => setSheet("history")} />
						)}
					</div>
				</div>
			</div>

			<Sheet open={sheet === "description"} onOpenChange={close} title={posting.title} description="The employer's page as they wrote it. Scripts and links in it do not run." size="wide">
				{sheet === "description" ? <iframe title="The employer's description" sandbox="" srcDoc={frameDocument(detail.descriptionHtml)} className="sheet-frame" /> : null}
			</Sheet>
			<Sheet open={sheet === "changed"} onOpenChange={close} title="What changed" description="Every Filter Decision recorded for this Posting, oldest first. Replays stay on record.">
				<div className="sheet-notes">
					{detail.decisions.map((decision) => (
						<DecisionNote key={decision.id} decision={decision} />
					))}
				</div>
			</Sheet>
			<Sheet open={sheet === "history"} onOpenChange={close} title="History" description="What happened to this application, oldest first.">
				<div className="sheet-notes">
					{detail.trackEvents.length === 0 ? <p className="form-help">Nothing has happened to this application yet.</p> : null}
					{detail.trackEvents.map((event) => (
						<TrackNote key={event.id} event={event} />
					))}
				</div>
			</Sheet>
			<Sheet open={sheet === "requirements"} onOpenChange={close} title="Requirements to check" description="The assessment read these in the description, but their wording is not on the page word for word, so they carry no underline.">
				<div className="sheet-notes">
					{margin.unplaced.map((entry) => (
						<div key={entry.clause} className="note">
							<span className="note-title">{entry.title}</span>
							{entry.subtitle === null ? null : <span className="note-subtitle">{entry.subtitle}</span>}
							<span className="note-subtitle">{entry.clause}</span>
						</div>
					))}
				</div>
			</Sheet>
		</article>
	);
}
