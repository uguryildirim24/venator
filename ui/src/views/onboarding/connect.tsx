import { ChevronDown, CircleAlert, CircleCheck, LoaderCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import type { RuntimeLane, RuntimeProbeResponse, RuntimeState } from "../../../shared/onboarding.ts";
import { runtimeStateLabel } from "../../labels.ts";
import { chooseDocumentRuntime, OnboardingRequestError, probeRuntimes, readInstallSettings } from "../../onboarding/api.ts";
import { CONNECT, LINKS } from "./copy.ts";
import { JevKeyField, OutsideLink } from "./parts.tsx";
import { readyLane } from "./runtime-selection.ts";

/**
 * The two assistants a person can connect, and the buttons they are.
 *
 * **Neither button is a login and the copy never says it is.** Venator holds no credential and
 * signs nobody in: `src/venator/llm/` starts a program that is already on this Mac and reads
 * what it says back. **Neither carries a vendor logo** — no permission to use either mark has
 * been given — so each names its assistant in plain text on the vendor's own colour, and those
 * colours stop at the edge of the button (ui/DESIGN.md, "Still true").
 */
type Vendor = {
	readonly lane: "claude" | "codex";
	readonly face: "claude" | "chatgpt";
	readonly button: string;
	readonly line: string;
	readonly install: string;
};

const VENDORS: readonly Vendor[] = [
	{ lane: "claude", face: "claude", button: CONNECT.claudeButton, line: CONNECT.claudeLine, install: LINKS.claudeInstall },
	{ lane: "codex", face: "chatgpt", button: CONNECT.chatgptButton, line: CONNECT.chatgptLine, install: LINKS.codexInstall },
];

/** States whose next step is getting the program onto this Mac, so the card links the installer. */
const INSTALL_STATES: readonly RuntimeState[] = ["not_installed", "not_spawnable"];

type Outcome =
	| { readonly kind: "idle" }
	| { readonly kind: "checking" }
	| { readonly kind: "answered"; readonly lane: RuntimeLane; readonly chosen: boolean }
	| { readonly kind: "unchecked"; readonly message: string };

const IDLE: Outcome = { kind: "idle" };

function refusalText(error: Error): string {
	if (!(error instanceof OnboardingRequestError)) return error.message;
	return error.remedy === null ? error.message : `${error.message} ${error.remedy}`;
}

/** What one press found, under its button. The probe's remedy and caveat arrive verbatim. */
function CardStatus({ outcome, install }: { readonly outcome: Outcome; readonly install: string }) {
	if (outcome.kind === "idle") return null;
	if (outcome.kind === "checking") {
		return (
			<p className="vendor-status" role="status">
				<LoaderCircle aria-hidden="true" className="icon spinner" data-tone="plain" />
				{CONNECT.checking}
			</p>
		);
	}
	if (outcome.kind === "unchecked") {
		return (
			<div role="status">
				<p className="vendor-status">
					<CircleAlert aria-hidden="true" className="icon" data-tone="plain" />
					{CONNECT.cannotCheck}
				</p>
				<p className="vendor-fix">{outcome.message}</p>
			</div>
		);
	}
	const { lane } = outcome;
	const connected = lane.ready && outcome.chosen;
	return (
		<div role="status">
			<p className="vendor-status">
				{connected ? (
					<CircleCheck aria-hidden="true" className="icon" data-tone="green" />
				) : (
					<CircleAlert aria-hidden="true" className="icon" data-tone="plain" />
				)}
				{runtimeStateLabel(lane.state)}
			</p>
			{lane.state === "failed" && lane.detail !== null ? <p className="vendor-fix">{lane.detail}</p> : null}
			{lane.remedy === null ? null : (
				<p className="vendor-fix" data-remedy="">
					{lane.remedy}
					{INSTALL_STATES.includes(lane.state) ? (
						<>
							{" "}
							<OutsideLink href={install}>{CONNECT.howToInstall}</OutsideLink>
						</>
					) : null}
				</p>
			)}
			{lane.caveat === null || lane.caveat === "" ? null : <p className="vendor-fix">{lane.caveat}</p>}
		</div>
	);
}

type ConnectStepProps = {
	/** Told which assistant is connected and chosen, so the résumé step can offer its reading. */
	readonly onRuntime: (lane: string | null) => void;
	/** The assistant this session already connected, so coming Back shows it as connected. */
	readonly connected: string | null;
};

/**
 * Step one: connect an assistant, add the Jev key, or skip both.
 *
 * **Nothing here runs on render.** A check starts the program and spends a few words of the
 * person's own plan, so it happens only from a press. A check that answers ready saves that
 * assistant as this Install's choice (`POST /api/onboarding/settings`); one that does not
 * changes nothing and shows the probe's own next step. There is no fallback between the two
 * and this screen never offers one.
 */
export function ConnectStep({ onRuntime, connected }: ConnectStepProps) {
	const [outcomes, setOutcomes] = useState<Readonly<Record<string, Outcome>>>({});
	const [keyPresent, setKeyPresent] = useState(false);
	const [aboutOpen, setAboutOpen] = useState(false);
	const busy = Object.values(outcomes).some((outcome) => outcome.kind === "checking");

	const readSettings = useCallback(() => {
		readInstallSettings()
			.then((settings) => setKeyPresent(settings.jevKeyPresent))
			.catch(() => setKeyPresent(false));
	}, []);
	useEffect(readSettings, [readSettings]);

	const settle = useCallback((lane: string, outcome: Outcome) => {
		// One assistant is the choice. A ready answer for this one retires the other's
		// "Connected", because a run would no longer use it.
		const chosen = outcome.kind === "answered" && outcome.chosen;
		setOutcomes((current) =>
			Object.fromEntries(
				Object.entries({ ...current, [lane]: outcome }).map(([other, previous]) => [
					other,
					chosen && other !== lane && previous.kind === "answered" && previous.chosen ? IDLE : previous,
				]),
			),
		);
	}, []);

	const check = useCallback(
		(vendor: Vendor) => {
			if (busy) return;
			settle(vendor.lane, { kind: "checking" });
			probeRuntimes({ kind: "lane", lane: vendor.lane })
				.then(async (response: RuntimeProbeResponse) => {
					if (!response.probe.available) {
						settle(vendor.lane, { kind: "unchecked", message: response.probe.message ?? CONNECT.cannotCheck });
						return;
					}
					const lane = response.lanes.find((entry) => entry.lane === vendor.lane);
					if (lane === undefined) {
						settle(vendor.lane, { kind: "unchecked", message: CONNECT.cannotCheck });
						return;
					}
					const ready = readyLane(response, vendor.lane);
					if (ready === null) {
						settle(vendor.lane, { kind: "answered", lane, chosen: false });
						return;
					}
					await chooseDocumentRuntime(vendor.lane);
					settle(vendor.lane, { kind: "answered", lane, chosen: true });
					onRuntime(ready);
				})
				.catch((error: Error) => settle(vendor.lane, { kind: "unchecked", message: refusalText(error) }));
		},
		[busy, onRuntime, settle],
	);

	return (
		<>
			<div className="vendor-cards">
				{VENDORS.map((vendor) => {
					const outcome = outcomes[vendor.lane] ?? IDLE;
					return (
						<section key={vendor.lane} className="vendor-card" aria-label={vendor.button}>
							<button type="button" className="vendor-button" data-vendor={vendor.face} disabled={busy} onClick={() => check(vendor)}>
								{vendor.button}
							</button>
							<p className="vendor-line">{vendor.line}</p>
							{outcome.kind === "idle" && connected === vendor.lane ? (
								<p className="vendor-status" role="status">
									<CircleCheck aria-hidden="true" className="icon" data-tone="green" />
									{runtimeStateLabel("available")}
								</p>
							) : (
								<CardStatus outcome={outcome} install={vendor.install} />
							)}
						</section>
					);
				})}
			</div>

			<section className="setup-section" aria-label={CONNECT.jevKey}>
				<h2 className="group-header">
					{CONNECT.jevKey}
					<span className="group-aside">{keyPresent ? CONNECT.keySaved : CONNECT.optional}</span>
				</h2>
				<JevKeyField present={keyPresent} onSaved={readSettings} />
				<p className="setup-caption">
					{CONNECT.keyCaption} <OutsideLink href={LINKS.typesafeKeys}>{CONNECT.getKey}</OutsideLink>
				</p>
			</section>

			<section className="setup-section">
				<button type="button" className="disclosure-button" aria-expanded={aboutOpen} onClick={() => setAboutOpen((open) => !open)}>
					<ChevronDown aria-hidden="true" className="icon" />
					{CONNECT.about}
				</button>
				{aboutOpen ? (
					<ul className="about-group setup-group">
						{CONNECT.aboutLines.map((line) => (
							<li key={line}>{line}</li>
						))}
					</ul>
				) : null}
			</section>
		</>
	);
}
