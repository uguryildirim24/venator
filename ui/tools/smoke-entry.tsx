/**
 * Mounts the real dashboard into whatever DOM the caller has installed, waits for its
 * data to land, and hands back the rendered markup. Loaded through Vite so the JSX and
 * TypeScript are transformed the same way the browser bundle is.
 */

import { createRoot } from "react-dom/client";

import { App } from "../src/app.tsx";

export async function renderRoute(hash: string, settle: () => Promise<void>, linger = 0): Promise<string> {
	window.location.hash = hash;
	const container = document.createElement("div");
	document.body.append(container);
	const root = createRoot(container);
	root.render(<App />);
	await settle();
	// A screen that only speaks once a read has been slow for a while needs the while.
	if (linger > 0) await new Promise((resolve) => setTimeout(resolve, linger));
	const markup = container.innerHTML;
	root.unmount();
	container.remove();
	return markup;
}

/**
 * The same, with one button pressed before the markup is read.
 *
 * Some of this app's screens are deliberately empty until somebody acts — the runtime step
 * shows nothing a check could have told it until a check is asked for — so the only way to
 * assert what those screens say is to press the thing a person presses. The press is a real
 * click on a real button found by its own visible label: no handler is reached for and no
 * component is rendered in isolation, so a button that stopped being wired up fails here
 * exactly as it would in front of somebody.
 *
 * A missing button is thrown rather than returned as empty markup, because "the label moved"
 * and "the screen said nothing" are different failures and a caller cannot tell them apart
 * from an empty string.
 */
export async function renderAfterPress(
	hash: string,
	labels: string | readonly string[],
	settle: () => Promise<void>,
): Promise<string> {
	window.location.hash = hash;
	const container = document.createElement("div");
	document.body.append(container);
	const root = createRoot(container);
	root.render(<App />);
	try {
		await settle();
		const sequence = Array.isArray(labels) ? labels : [labels];
		for (const label of sequence) {
			const target = [...document.querySelectorAll("button")].find(
				(button) =>
					button.getAttribute("aria-label") === label || (button.textContent ?? "").trim() === label,
			);
			if (target === undefined) throw new Error(`no button on ${hash} is labelled "${label}"`);
			// Radix tabs activate on primary pointer-down; click alone does not exercise that path.
			target.dispatchEvent(new window.MouseEvent("mousedown", { bubbles: true, button: 0 }));
			target.click();
			await settle();
		}
		return document.body.innerHTML;
	} finally {
		root.unmount();
		container.remove();
	}
}
