import type { RuntimeProbeResponse } from "../../../shared/onboarding.ts";

/** A probe of another runtime never offers it as a substitute for the saved choice. */
export function readyLane(answer: RuntimeProbeResponse, selected: "claude" | "codex"): string | null {
	return answer.lanes.find((lane) => lane.lane === selected && lane.ready)?.lane ?? null;
}
