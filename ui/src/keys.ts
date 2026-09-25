/**
 * Keyboard handling for an owner who reviews dozens of Postings a day: every action has a
 * single-key binding, and bindings are declared next to the view that owns them so the help
 * overlay can list exactly what is live right now.
 */

import { useEffect, useMemo, useState } from "react";

export type KeyBinding = {
	readonly keys: readonly string[];
	readonly label: string;
	readonly description: string;
	readonly run: () => void;
};

function isTextEntry(target: EventTarget | null): boolean {
	if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) return true;
	return target instanceof HTMLElement && target.isContentEditable;
}

/**
 * Bindings fire on plain keypresses only. Modifier chords stay with the browser or the host
 * webview, and while the caret is in a text field every key but Escape belongs to that field.
 */
export function useKeyBindings(bindings: readonly KeyBinding[]): void {
	useEffect(() => {
		const onKeyDown = (event: KeyboardEvent) => {
			if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
			if (event.target instanceof HTMLElement && event.target.closest('[role="dialog"], [role="tablist"], select')) return;
			const typing = isTextEntry(event.target);
			if (typing && event.key !== "Escape") return;
			const binding = bindings.find((candidate) => candidate.keys.includes(event.key));
			if (binding === undefined) return;
			event.preventDefault();
			binding.run();
		};
		window.addEventListener("keydown", onKeyDown);
		return () => window.removeEventListener("keydown", onKeyDown);
	}, [bindings]);
}

/**
 * Selection state for a keyboard-driven list. Selection is clamped as results change, so a
 * search that shrinks the list never leaves the cursor pointing past the end.
 */
export function useListNavigation(count: number, onOpenIndex: (index: number) => void) {
	const [selected, setSelected] = useState(0);

	useEffect(() => {
		setSelected((current) => Math.min(current, Math.max(0, count - 1)));
	}, [count]);

	const bindings = useMemo<readonly KeyBinding[]>(
		() => [
			{
				keys: ["j", "ArrowDown"],
				label: "j",
				description: "next Posting",
				run: () => setSelected((current) => Math.min(current + 1, Math.max(0, count - 1))),
			},
			{
				keys: ["k", "ArrowUp"],
				label: "k",
				description: "previous Posting",
				run: () => setSelected((current) => Math.max(current - 1, 0)),
			},
			{ keys: ["g"], label: "g", description: "first Posting", run: () => setSelected(0) },
			{
				keys: ["G"],
				label: "G",
				description: "last Posting",
				run: () => setSelected(Math.max(0, count - 1)),
			},
			{
				keys: ["Enter", "o"],
				label: "enter",
				description: "open the selected Posting",
				run: () => {
					if (count > 0) onOpenIndex(selected);
				},
			},
		],
		[count, selected, onOpenIndex],
	);

	useKeyBindings(bindings);

	return [selected, setSelected] as const;
}
