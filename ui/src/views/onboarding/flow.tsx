import { useCallback, useMemo, useRef, useState } from "react";

import type { ProfileWriteResponse } from "../../../shared/onboarding.ts";
import { extractedFieldLabel } from "../../labels.ts";
import { OnboardingRequestError, writeProfile } from "../../onboarding/api.ts";
import { EMPTY_DRAFT, profileProposal, unconfirmedFields, unconfirmedSections, type Draft, type ResumeDraft, type TargetingDraft } from "../../onboarding/draft.ts";
import { navigate, onboardingHash, queueHash, type OnboardingStep } from "../../router.ts";
import { planRun, startRun } from "../../runs/api.ts";
import { ConnectStep } from "./connect.tsx";
import { CONFIRM_CONTACT, CONNECT, missingContactNote, REPLACE, RESUME, SETUP, SETUP_WINDOW_TITLE, stepCaption, TARGETING } from "./copy.ts";
import { ResumeStep, type ResumeSource } from "./resume.tsx";
import { TargetingStep } from "./targeting.tsx";

/** The Profile a first setup writes when the Install has none to replace. */
const FIRST_PROFILE = "main";

const HEADINGS = {
	runtime: { index: 1, title: CONNECT.title, lede: CONNECT.lede },
	resume: { index: 2, title: RESUME.title, lede: RESUME.lede },
	targeting: { index: 3, title: TARGETING.title, lede: TARGETING.lede },
} satisfies Record<OnboardingStep, { readonly index: number; readonly title: string; readonly lede: string }>;

type FlowProps = {
	readonly step: OnboardingStep;
	/** The Profile this Install runs, if it has one: setting up again offers to replace it. */
	readonly implied: string | null;
	/** Called once a Profile has been written, with what the write said. */
	readonly onWritten: (response: ProfileWriteResponse) => void;
};

type Primary = { readonly label: string; readonly held: boolean; readonly onPress: () => void };

type Footer = {
	readonly onSkip: () => void;
	readonly back: OnboardingStep | null;
	readonly note: string | null;
	readonly primary: Primary;
};

/**
 * Setting up Venator: connect an assistant, add a résumé, choose what jobs to find.
 *
 * - **The draft lives here.** Each step edits part of one value, and nothing is written until
 *   the last step, so Back and Skip for Now cost nothing.
 * - **The step is in the hash**, so Back works and every screen renders on its own in
 *   `pnpm smoke`.
 * - **Skip for Now is on every step.** On the résumé step it leaves the résumé out of the
 *   Profile; on the last it saves the Profile without finding jobs yet.
 * - **Find Jobs writes the Profile and starts the Refresh** the sidebar would start: the same
 *   plan, the same token, nothing the dashboard could not do from its own button.
 */
export function OnboardingFlow({ step, implied, onWritten }: FlowProps) {
	const [draft, setDraft] = useState<Draft>(() => ({
		...EMPTY_DRAFT,
		targeting: { ...EMPTY_DRAFT.targeting, profileName: implied ?? FIRST_PROFILE },
	}));
	const [resumeFile, setResumeFile] = useState<File | null>(null);
	const [source, setSource] = useState<ResumeSource | null>(null);
	const [resumeSkipped, setResumeSkipped] = useState(false);
	const [readingResume, setReadingResume] = useState(false);
	/** The assistant connected this session. Not in the draft: no Profile holds a runtime. */
	const [lane, setLane] = useState<string | null>(null);
	const [writing, setWriting] = useState(false);
	const [failure, setFailure] = useState<OnboardingRequestError | null>(null);
	/** What the last press asked for, so Replace It finishes the same way. */
	const findingJobs = useRef(false);

	const setResume = useCallback((resume: ResumeDraft) => setDraft((current) => ({ ...current, resume })), []);
	const setTargeting = useCallback((targeting: TargetingDraft) => setDraft((current) => ({ ...current, targeting })), []);
	const onRead = useCallback((resume: ResumeDraft, read: ResumeSource, file: File | null) => {
		setDraft((current) => ({ ...current, resume }));
		setSource(read);
		setResumeFile(file);
	}, []);

	const contactMissing = useMemo(() => unconfirmedFields(draft.resume), [draft.resume]);
	const withResume = !resumeSkipped && source !== null && contactMissing.length === 0;

	const write = useCallback(
		(findJobs: boolean, overwrite: boolean) => {
			if (writing) return;
			findingJobs.current = findJobs;
			setWriting(true);
			setFailure(null);
			writeProfile(profileProposal(draft, overwrite, withResume), withResume ? resumeFile : null)
				.then(async (response) => {
					if (findJobs) {
						// The sidebar's Refresh, pressed once on the person's behalf because they
						// pressed Find Jobs. A plan that cannot start leaves the first-run screen
						// to say why, with its own Refresh.
						const answer = await planRun("fetch-and-filter").catch(() => null);
						if (answer?.plan.startable === true) await startRun(answer.plan.token).catch(() => null);
					}
					onWritten(response);
					navigate(queueHash());
				})
				.catch((error: Error) => {
					setFailure(error instanceof OnboardingRequestError ? error : new OnboardingRequestError(error.message, null, null, null));
				})
				.finally(() => setWriting(false));
		},
		[draft, onWritten, resumeFile, withResume, writing],
	);

	const footer: Footer = useMemo(() => {
		if (step === "runtime") {
			const next = () => navigate(onboardingHash("resume"));
			return { onSkip: next, back: null, note: null, primary: { label: SETUP.continue, held: false, onPress: next } };
		}
		if (step === "resume") {
			const onward = (skipped: boolean) => () => {
				setResumeSkipped(skipped);
				navigate(onboardingHash("targeting"));
			};
			const empty = contactMissing.filter((field) => draft.resume.values[field].trim() === "");
			const note =
				source === null
					? RESUME.addFirst
					: empty.length > 0
						? missingContactNote(empty.map(extractedFieldLabel))
						: contactMissing.length > 0
							? CONFIRM_CONTACT
							: unconfirmedSections(draft.resume).length > 0
								? RESUME.onlyTicked
								: null;
			return {
				onSkip: onward(true),
				back: "runtime",
				note,
				primary: { label: SETUP.continue, held: readingResume || source === null || contactMissing.length > 0, onPress: onward(false) },
			};
		}
		return {
			onSkip: () => write(false, false),
			back: "resume",
			note: draft.targeting.boards.length === 0 ? TARGETING.addEmployerFirst : null,
			primary: {
				label: writing ? SETUP.saving : SETUP.findJobs,
				held: writing || draft.targeting.boards.length === 0,
				onPress: () => write(true, false),
			},
		};
	}, [contactMissing, draft.resume, draft.targeting.boards.length, readingResume, source, step, write, writing]);

	const heading = HEADINGS[step];
	const tight = step === "targeting" || (step === "resume" && source !== null);

	return (
		<div className="setup">
			<header className="setup-titlebar" data-tauri-drag-region>
				{SETUP_WINDOW_TITLE}
			</header>
			<div className="setup-body">
				<div className="setup-column" data-density={tight ? "tight" : undefined}>
					<header className="setup-heading">
						<p className="setup-step">{stepCaption(heading.index)}</p>
						<h1 className="setup-title">{heading.title}</h1>
						<p className="setup-lede">{heading.lede}</p>
					</header>
					{step === "runtime" ? <ConnectStep onRuntime={setLane} connected={lane} /> : null}
					{step === "resume" ? <ResumeStep draft={draft.resume} source={source} lane={lane} onRead={onRead} onChange={setResume} onReading={setReadingResume} /> : null}
					{step === "targeting" ? <TargetingStep draft={draft.targeting} onChange={setTargeting} /> : null}
					{step === "targeting" && failure !== null ? (
						<div className="setup-alert" role="alert">
							<p>{failure.code === "profile_exists" ? REPLACE.exists : failure.message}</p>
							{failure.remedy === null || failure.code === "profile_exists" ? null : <p>{failure.remedy}</p>}
							{failure.code === "profile_exists" ? (
								<div>
									<button type="button" className="button" data-size="large" disabled={writing} onClick={() => write(findingJobs.current, true)}>
										{REPLACE.replace}
									</button>
								</div>
							) : null}
						</div>
					) : null}
				</div>
			</div>
			<footer className="setup-footer">
				<button type="button" className="button" data-size="large" data-tone="borderless" disabled={writing} onClick={footer.onSkip}>
					{SETUP.skip}
				</button>
				{footer.note === null ? null : <p className="setup-note">{footer.note}</p>}
				{footer.back === null ? null : (
					<button type="button" className="button" data-size="large" onClick={() => navigate(onboardingHash(footer.back ?? "runtime"))}>
						{SETUP.back}
					</button>
				)}
				<button type="button" className="button" data-size="large" data-tone="prominent" disabled={footer.primary.held} onClick={footer.primary.onPress}>
					{footer.primary.label}
				</button>
			</footer>
		</div>
	);
}
