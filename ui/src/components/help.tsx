import type { ShortcutHelp } from "../shortcuts.ts";
import { Sheet } from "./sheet.tsx";

type HelpProps = {
	readonly shortcuts: readonly ShortcutHelp[];
	readonly open: boolean;
	readonly onOpenChange: (open: boolean) => void;
};

/**
 * Every keyboard shortcut in the app, as one sheet. `?` opens and closes it, and Escape or Done
 * dismisses it. A shortcut label is one caption that may name two keys ("j / ↓"), so it is set
 * as one keycap rather than split on a guess about the separator.
 */
export function HelpSheet({ shortcuts, open, onOpenChange }: HelpProps) {
	return (
		<Sheet open={open} onOpenChange={onOpenChange} title="Keyboard" description="Every shortcut the dashboard listens for. None of them changes a Posting.">
			<dl className="shortcuts">
				{shortcuts.map((shortcut) => (
					<div key={shortcut.label} className="shortcut">
						<dt>
							<kbd className="kbd">{shortcut.label}</kbd>
						</dt>
						<dd>{shortcut.description}</dd>
					</div>
				))}
			</dl>
		</Sheet>
	);
}
