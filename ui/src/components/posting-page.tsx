import { Check, ChevronRight, Loader, Lock } from "lucide-react";
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { FilterDecision, PostingDetail, TrackEvent } from "../../shared/contracts.ts";
import { applicationFileUrl, saveAnswer, saveAuthorization, type ApplicationEdit, type ApplicationManifest } from "../api.ts";
import { preparation, type ApplicationControl } from "../application.ts";
import { formatDay, formatMoment, hostOf } from "../format.ts";
import { assessmentNoteLabel, decisionTitle, employerLabel, ruleLabel, sourceLabel, statusLabel, trackEventLabel } from "../labels.ts";
import { buildMargin, type Margin, type MarginNote, type MarkedBlock } from "../margin.ts";
import { latestHardFilter, triageAllowed } from "../triage.ts";
import { ExternalLink } from "./external-link.tsx";
import { PageBlockText } from "./page-block-text.ts";
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

function Block({ block, onOpen, index }: { readonly block: MarkedBlock; readonly onOpen: () => void; readonly index: number }) {
	const text =
		block.kind === "heading" ? (
			<h2 className="page-heading">
				<PageBlockText block={block} />
			</h2>
		) : (
			<p className={block.kind === "item" ? "page-text page-item" : "page-text"}>
				<PageBlockText block={block} />
			</p>
		);
	return (
		<div className="page-block" data-kind={block.kind} role={block.kind === "item" ? "listitem" : undefined}>
			<div id={`posting-line-${index}`} className="page-cell">{text}</div>
			<div className="margin-cell">
				<Notes notes={block.notes} count={block.count} onOpen={onOpen} />
			</div>
		</div>
	);
}

/** Consecutive list items, as one list. The list and its items lay out as the grid's own rows. */
function PageBlocks({ blocks, onOpen }: { readonly blocks: readonly MarkedBlock[]; readonly onOpen: () => void }) {
	let offset = 0;
	const runs: MarkedBlock[][] = [];
	for (const block of blocks) {
		const last = runs.at(-1);
		if (block.kind === "item" && last?.[0]?.kind === "item") last.push(block);
		else runs.push([block]);
	}
	return (
		<>
			{runs.map((run, index) => {
				const start = offset;
				offset += run.length;
				return run[0]?.kind === "item" ? (
					<div key={index} className="page-list" role="list">
						{run.map((block, item) => (
							<Block key={item} block={block} onOpen={onOpen} index={start + item} />
						))}
					</div>
				) : (
					run.map((block) => <Block key={index} block={block} onOpen={onOpen} index={start} />)
				);
			})}
		</>
	);
}

/** Why there is no page to read, in words. */
function emptyPageReason(detail: PostingDetail): string {
	const kind = detail.assessment?.descriptionKind;
	if (kind === "snippet") return "Only a snippet of the description was fetched, and it has no text to set here.";
	if (detail.descriptionHtml.trim() !== "") return "No readable text in this description.";
	return "The full description has not been fetched yet. It arrives with the next detail check of this listing.";
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

function QuickLook({ postingKey, manifest, open, busy, error, onOpenChange, onEdit }: { readonly postingKey: string; readonly manifest: ApplicationManifest; readonly open: boolean; readonly busy: boolean; readonly error: string | null; readonly onOpenChange: (open: boolean) => void; readonly onEdit: (edits: readonly ApplicationEdit[]) => void }) {
	const [view, setView] = useState<"pdf" | "text" | "letter">("pdf");
	const [edits, setEdits] = useState<Record<string, string>>({});
	const [answers, setAnswers] = useState<Record<string, string>>({});
	const [answerError, setAnswerError] = useState<string | null>(null);
	useEffect(() => setEdits({}), [manifest.version]);
	const pdf = applicationFileUrl(postingKey, "resume.pdf");
	return (
		<Sheet open={open} onOpenChange={onOpenChange} title="Application" size="wide">
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
			{view === "letter" ? <iframe key={manifest.version} src={applicationFileUrl(postingKey, "letter.pdf")} title="Cover letter" className="sheet-frame" /> : null}
			{manifest.formQuestions?.filter((row) => row.kind !== "file").map((row) => (
				<div key={row.name} className="form-row">
					<span>{row.label}</span>
					{row.answer === null ? row.category === "authorization" ? <div className="empty-actions">
						{([true, false] as const).map((answer) => <button key={String(answer)} type="button" className="button" onClick={() => {
							void saveAuthorization(postingKey, answer).then(() => setAnswerError(null)).catch((failure: Error) => setAnswerError(failure.message));
						}}>{answer ? "Yes" : "No"}</button>)}
					</div> : row.bucket === "reserved" && row.category !== "eeo" ? null : <>
						<input className="text-field" value={answers[row.name] ?? ""} onChange={(event) => setAnswers((previous) => ({ ...previous, [row.name]: event.target.value }))} />
						<button type="button" className="button" disabled={!answers[row.name]?.trim()} onClick={() => {
							void saveAnswer(postingKey, { text: row.label, kind: row.kind === "select" || row.kind === "textarea" ? row.kind : "text",
								answer: answers[row.name] ?? "", universal: false, eeo: row.category === "eeo" }).then(() => setAnswerError(null)).catch((error: Error) => setAnswerError(error.message));
						}}>Keep</button>
					</> : <strong>{row.answer}</strong>}
				</div>
			))}
			{manifest.draftProvenance?.filter((row) => row.final !== null).map((row) => (
				<label key={row.draft_id} className="form-row">
					<span>{row.kind === "resume_bullet" ? "Résumé bullet" : row.kind === "essay_answer" ? "Essay" : "Letter paragraph"}</span>
					<textarea maxLength={1800} value={edits[row.draft_id] ?? row.final ?? ""} onChange={(event) => setEdits((previous) => ({ ...previous, [row.draft_id]: event.target.value }))} />
				</label>
			))}
			{error || answerError ? <p role="alert">{error ?? answerError}</p> : null}
			{Object.keys(edits).length ? <button type="button" className="button" disabled={busy} onClick={() => onEdit(Object.entries(edits).map(([draft_id, text]) => ({ draft_id, text })))}>Save edits</button> : null}
			<div className="empty-actions">
				<a className="button" href={pdf} download>Download résumé</a>
				{manifest.letterText ? <a className="button" href={applicationFileUrl(postingKey, "letter.pdf")} download>Download letter</a> : null}
			</div>
		</Sheet>
	);
}

function ApplicationMargin({ detail, control, onHistory }: { readonly detail: PostingDetail; readonly control: ApplicationControl; readonly onHistory: () => void }) {
	const [quickLook, setQuickLook] = useState(false);
	const [kept, setKept] = useState<ReadonlySet<string>>(new Set());
	const [captureError, setCaptureError] = useState<string | null>(null);
	const [awaitingReview, setAwaitingReview] = useState(false);
	useLayoutEffect(() => {
		if (control.busy === "apply") setAwaitingReview(true);
		if (!awaitingReview || control.busy !== null) return;
		if (control.error !== null) { setAwaitingReview(false); return; }
		if (control.reviewVersion !== null && control.manifest.status === "ready" &&
			control.manifest.value.version === control.reviewVersion) {
			setQuickLook(true);
			setAwaitingReview(false);
		}
	}, [control.busy, control.manifest, control.error, control.reviewVersion, awaitingReview]);
	const state = detail.application?.state ?? null;
	const since = detail.application?.since ?? null;
	const allowed = detail.status !== "hard-killed" && preparation(detail).allowed;
	const manifest = control.manifest.status === "ready" ? control.manifest.value : null;

	let body: ReactNode;
	if (!allowed && state !== "submitted" && state !== "concluded") {
		body = null;
	} else if (control.busy === "apply") {
		body = (
			<div className="resume-state">
				<Loader aria-hidden="true" className="glyph spinner" />
				<span>Applying…</span>
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
						<strong>Application</strong>
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
					{control.busy === null ? <button type="button" className="button" onClick={() => control.run("applied")}>I Applied</button> : <span className="resume-state">Saving…</span>}
				</div>
				<button type="button" className="button" disabled={control.busy !== null} onClick={() => control.run("fill")}>{control.busy === "fill" ? "Opening…" : "Fill"}</button>
				<QuickLook postingKey={detail.posting.key} manifest={manifest} open={quickLook} busy={control.busy !== null} error={control.failedAction === "edit" ? control.error : null} onOpenChange={setQuickLook} onEdit={(edits) => { if (manifest.version) control.run("edit", { version: manifest.version, edits }); }} />
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
					{control.busy === null && control.failedAction === "apply" ? <button type="button" className="button" onClick={() => { control.run("apply"); setAwaitingReview(true); }}>Retry Apply</button> : null}
				</div>
			</>
		);
	}

	return (
		<div className="margin-application">
			<ExternalLink className="button" size="large" href={detail.posting.url} title={detail.posting.url}>Open Listing</ExternalLink>
			{body}
			{manifest?.candidates?.some((candidate) => !kept.has(candidate.name)) ? <div className="note"><span className="note-title">Keep these answers?</span>
				{manifest.candidates.filter((candidate) => !kept.has(candidate.name)).map((candidate) => <button key={candidate.name} type="button" className="note-action" onClick={() => {
					void saveAnswer(detail.posting.key, { text: candidate.label, kind: "text", answer: candidate.value,
						universal: false }).then(() => setKept((previous) => new Set([...previous, candidate.name])))
						.catch((failure: Error) => setCaptureError(failure.message));
				}}>{candidate.label}: {String(candidate.value)}</button>)}
				{captureError === null ? null : <span role="alert">{captureError}</span>}
			</div> : null}
			{allowed && control.manifest.status === "error" ? (
				<div className="note">
					<span className="note-title">Documents could not be read</span>
					<span className="note-subtitle">{control.manifest.message}</span>
				</div>
			) : null}
			{allowed && control.busy === null && control.failedAction === "apply" && control.prepared ? <button type="button" className="button" onClick={() => { control.run("apply"); setAwaitingReview(true); }}>Retry Apply</button> : null}
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
			{allowed ? control.warnings.map((warning) => (
				<div key={warning} className="note">
					<span className="note-subtitle">{warning}</span>
				</div>
			)) : null}
		</div>
	);
}

/* ----------------------------------------------------------------- the page */

function FitLedger({ fit, fallback, onJump }: { readonly fit: Margin["fit"]; readonly fallback: number | null; readonly onJump: (at: number) => void }) {
	if (fit === null) return null;
	return <div className="fit-ledger">
		{fit.count === null ? null : <span className="fit-count">{fit.count}</span>}
		{fit.tally.length ? <div className="fit-tally" aria-hidden="true">{fit.tally.map((tone, index) => <span key={index} data-tone={tone} />)}</div> : null}
		{fit.groups.map((group) => <button key={group.tone} type="button" className="note fit-group" data-tone={group.tone} onClick={() => { const at = group.at ?? fallback; if (at !== null) onJump(at); }}>
			<span className="note-title">{group.title}</span>
			{group.clauses.map((clause) => <span key={clause} className="note-subtitle" title={clause}>{clause}</span>)}
		</button>)}
	</div>;
}

type PostingPageProps = {
	readonly detail: PostingDetail;
	readonly control: ApplicationControl;
	readonly arriving: boolean;
};

/**
 * The Posting as a reading page, with Venator's notes level with the lines they are about.
 *
 * Every block sits in its own row of one grid, its notes in column two of that row, so a note
 * lines up with its text without anything being measured. The page column holds only the
 * employer's words, as plain text from the reader extraction; the margin holds only Venator's.
 */
export function PostingPage({ detail, control, arriving }: PostingPageProps) {
	const [sheet, setSheet] = useState<"changed" | "history" | "requirements" | null>(null);
	const [expanded, setExpanded] = useState(false);
	const jumpTo = useRef<number | null>(null);
	const pageRef = useRef<HTMLElement>(null);
	useLayoutEffect(() => {
		if (!expanded || jumpTo.current === null) return;
		const at = jumpTo.current;
		jumpTo.current = null;
		const frame = requestAnimationFrame(() => pageRef.current?.querySelector(`#posting-line-${at}`)?.scrollIntoView({ block: "start" }));
		return () => cancelAnimationFrame(frame);
	}, [expanded]);
	const openLine = (at: number) => {
		if (expanded) pageRef.current?.querySelector(`#posting-line-${at}`)?.scrollIntoView({ block: "start" });
		else { jumpTo.current = at; setExpanded(true); }
	};

	// Keep the margin a single flowing column without letting it change the employer's
	// paragraph heights. Reflow when either column changes size (including loaded documents).
	useLayoutEffect(() => {
		const page = pageRef.current;
		if (page === null) return;
		let frame = 0;
		const layout = () => {
			const cells = [...page.querySelectorAll<HTMLElement>(".page-block > .margin-cell")]
				.filter((cell) => expanded || !cell.closest('.description-blocks:not([data-expanded="true"])'));
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
	}, [detail, control.manifest.status, control.prepared, expanded]);
	const { posting, assessment } = detail;
	const margin = useMemo(
		() =>
			buildMargin({
				page: detail.page,
				decisions: detail.decisions,
				assessment,
				keepProbability: detail.status === "hard-killed" ? detail.keepProbability : null,
			}),
		[detail, assessment],
	);
	const excluded = !triageAllowed(detail, latestHardFilter(detail));
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
		<article ref={pageRef} className="posting" data-arriving={arriving || undefined} aria-label={posting.title}>
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
						<FitLedger fit={margin.fit} fallback={margin.requirementsAt} onJump={openLine} />
						{excluded ? null : <ApplicationMargin detail={detail} control={control} onHistory={() => setSheet("history")} />}
						{margin.header.map((note) => (
							<Note key={note.key} note={note} onOpen={openRequirements} />
						))}
					</div>
				</div>
			</div>

			<div className="description-blocks" data-expanded={expanded || margin.blocks.length === 0}>
				{margin.blocks.length === 0 ? (
					<div className="page-block" data-kind="paragraph">
						<div className="page-cell"><p className="page-empty">{emptyPageReason(detail)}</p></div>
						<div className="margin-cell" />
					</div>
				) : <PageBlocks blocks={margin.blocks} onOpen={openRequirements} />}
			</div>

			<div className="page-block" data-kind="foot">
				<div className="page-cell page-foot">
					{margin.blocks.length === 0 ? null : <button type="button" className="button" data-size="large" onClick={() => setExpanded(!expanded)}>{expanded ? "Read Less" : "Read More"}</button>}
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
