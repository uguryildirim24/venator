/** What the keyboard sheet lists. The bindings themselves live with the view that owns them. */
export type ShortcutHelp = {
	readonly label: string;
	readonly description: string;
};

export const SHORTCUTS: readonly ShortcutHelp[] = [
	{ label: "1", description: "For you" },
	{ label: "2", description: "Filter inspector" },
	{ label: "3", description: "Early review" },
	{ label: "j / ↓", description: "Next Posting" },
	{ label: "k / ↑", description: "Previous Posting" },
	{ label: "g / G", description: "First / last Posting" },
	{ label: "enter", description: "Open the selected row, in the inspector" },
	{ label: "/", description: "Search" },
	{ label: "esc", description: "Leave the search, or clear it" },
	{ label: "r", description: "Reload from the view database" },
	{ label: "?", description: "This list" },
];
