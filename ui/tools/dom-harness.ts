/**
 * Shared harness for the two tools that render the dashboard outside a browser: the smoke
 * assertions and the HTML snapshots. It installs a DOM, points relative /api URLs at a
 * running server, and loads the app through Vite so JSX and TypeScript are transformed
 * exactly as they are for the browser bundle.
 */

import { Window } from "happy-dom";
import { createServer, type ViteDevServer } from "vite";

/** The smoke waits for its fixture API, not for a fixed guess at machine speed. */
const IDLE_MS = 200;
const SETTLE_TIMEOUT_MS = 15_000;

function delay(milliseconds: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/** Object.defineProperty is used because several of these exist on the Node global as getters. */
function defineGlobal(name: string, value: PropertyDescriptor["value"]): void {
	Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
}

/**
 * A host to render as, for the checks that are about the host rather than about the data.
 *
 * The desktop shell is not a theme the app can be asked for — it reads the window it was
 * given, once, at import: `isTauri` for whether this is the Tauri host at all and the user
 * agent for which operating system that host is on. Both are stated here, before the module
 * graph is loaded, so a pass can render the real shell as a Mac or as a Windows machine and
 * assert what it draws. Omitted, the harness installs a plain browser exactly as before.
 */
export type Host = {
	readonly tauri: boolean;
	readonly userAgent: string;
};

export type FixtureResponse = (input: RequestInfo | URL, init?: RequestInit) => Response | null;

export function installDom(origin: string, host?: Host, fixtureResponse?: FixtureResponse): () => Promise<void> {
	const dom =
		host === undefined
			? new Window({ url: `${origin}/` })
			: new Window({ url: `${origin}/`, settings: { navigator: { userAgent: host.userAgent } } });
	if (host?.tauri === true) {
		// Tauri v2 sets this on the window before any app code runs; `src/platform.ts` reads it.
		Object.defineProperty(dom, "isTauri", { configurable: true, value: true });
	}
	dom.document.body.innerHTML = '<div id="root"></div>';

	// happy-dom has no layout engine, and the list scrolls its selection into view.
	dom.HTMLElement.prototype.scrollIntoView = () => undefined;

	defineGlobal("window", dom);
	defineGlobal("document", dom.document);
	defineGlobal("location", dom.location);
	defineGlobal("history", dom.history);
	defineGlobal("navigator", dom.navigator);
	defineGlobal("Element", dom.Element);
	defineGlobal("Node", dom.Node);
	defineGlobal("NodeFilter", dom.NodeFilter);
	defineGlobal("HTMLButtonElement", dom.HTMLButtonElement);
	defineGlobal("SVGElement", dom.SVGElement);
	defineGlobal("ResizeObserver", dom.ResizeObserver);
	defineGlobal("HTMLElement", dom.HTMLElement);
	defineGlobal("HTMLInputElement", dom.HTMLInputElement);
	defineGlobal("HTMLTextAreaElement", dom.HTMLTextAreaElement);
	defineGlobal("Event", dom.Event);
	defineGlobal("CustomEvent", dom.CustomEvent);
	defineGlobal("HashChangeEvent", dom.HashChangeEvent);
	defineGlobal("MutationObserver", dom.MutationObserver);
	defineGlobal("getComputedStyle", dom.getComputedStyle);
	defineGlobal("requestAnimationFrame", dom.requestAnimationFrame);
	defineGlobal("cancelAnimationFrame", dom.cancelAnimationFrame);

	// The app builds relative /api URLs, which Node's fetch cannot resolve on its own; the
	// harness supplies the origin the browser would have taken from the page.
	const nodeFetch = globalThis.fetch;
	const pending = new Map<symbol, string>();
	let lastActivity = Date.now();
	globalThis.fetch = (input, init) => {
		const fixture = fixtureResponse?.(input, init);
		if (fixture !== undefined && fixture !== null) return Promise.resolve(fixture);
		const url = new URL(String(input), origin).toString();
		const request = Symbol();
		pending.set(request, url);
		lastActivity = Date.now();
		return nodeFetch(url, init).finally(() => {
			pending.delete(request);
			lastActivity = Date.now();
		});
	};
	return async () => {
		// Give React a turn to mount or handle the press before checking network idleness.
		await delay(100);
		const deadline = Date.now() + SETTLE_TIMEOUT_MS;
		while (pending.size > 0 || Date.now() - lastActivity < IDLE_MS) {
			if (Date.now() >= deadline) throw new Error(`the fixture API did not settle: ${[...pending.values()].join(", ")}`);
			await delay(25);
		}
	};
}

export type RouteRenderer = {
	/** Render and read the screen, `linger` milliseconds after the API has gone quiet. */
	readonly render: (hash: string, linger?: number) => Promise<string>;
	/** Render, press the button carrying this visible label, and read what the screen became. */
	readonly press: (hash: string, label: string | readonly string[]) => Promise<string>;
	readonly close: () => Promise<void>;
};

export async function startRenderer(origin: string, host?: Host, fixtureResponse?: FixtureResponse): Promise<RouteRenderer> {
	const settle = installDom(origin, host, fixtureResponse);
	const vite: ViteDevServer = await createServer({
		server: { middlewareMode: true, hmr: false },
		appType: "custom",
		logLevel: "warn",
	});
	const entry = await vite.ssrLoadModule("/tools/smoke-entry.tsx");
	const renderRoute = entry.renderRoute;
	const renderAfterPress = entry.renderAfterPress;
	return {
		render: (hash: string, linger?: number) => renderRoute(hash, settle, linger),
		press: (hash: string, label: string | readonly string[]) => renderAfterPress(hash, label, settle),
		close: () => vite.close(),
	};
}
