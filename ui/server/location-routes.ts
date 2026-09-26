import { Hono } from "hono";
import { cors } from "hono/cors";

import { readFromView } from "./db.ts";
import { checkLocalAction, LOCAL_ACTION_ORIGINS } from "./http/local-action-guard.ts";
import { availableLocations, readLocationSelection, saveLocationSelection } from "./location-filter.ts";
import { asList, asMapping, asText, at, parseJson } from "./onboarding/json.ts";
import { ensureReadableView } from "./view-recovery.ts";

/** The only write is this Install's view preference, not a pipeline store. */
export function createLocationRoutes(): Hono {
	const routes = new Hono();
	routes.use("*", cors({ origin: [...LOCAL_ACTION_ORIGINS], allowMethods: ["GET", "POST", "OPTIONS"], allowHeaders: ["Content-Type", "X-Venator-Location"] }));
	routes.get("/", async (context) => {
		await ensureReadableView();
		return context.json(readFromView(({ database }) => ({ selected: readLocationSelection(), choices: availableLocations(database) })));
	});
	routes.post("/", async (context) => {
		const refusal = checkLocalAction("X-Venator-Location", "1", context.req.header("X-Venator-Location"), context.req.header("origin"));
		if (refusal !== null) return context.json({ error: refusal.message }, 403);
		const declared = Number(context.req.header("content-length"));
		if (declared > 128 * 1024) return context.json({ error: "Location choice is too large." }, 413);
		const payload = await context.req.text();
		if (payload.length > 128 * 1024) return context.json({ error: "Location choice is too large." }, 413);
		let selected: string[] | null = null;
		try {
			const mapping = asMapping(parseJson(payload));
			const values = mapping === null ? null : asList(at(mapping, "selected"));
			if (values !== null && values.length <= 500) {
				const parsed = values.map(asText);
				if (parsed.every((item) => item !== null && item.length < 256)) selected = parsed.filter((item) => item !== null);
			}
		} catch { /* Invalid JSON is refused below. */ }
		if (selected === null) return context.json({ error: "Choose locations from the list." }, 400);
		const choices = readFromView(({ database }) => availableLocations(database));
		const allowed = new Set(choices.map((choice) => choice.key));
		if (selected.some((key) => !allowed.has(key))) return context.json({ error: "Choose locations from the list." }, 400);
		await saveLocationSelection([...new Set(selected)]);
		return context.json({ selected: readLocationSelection(), choices });
	});
	return routes;
}
