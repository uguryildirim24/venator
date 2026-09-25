import { Dialog } from "radix-ui";
import type { ReactNode } from "react";

type SheetProps = {
	readonly open: boolean;
	readonly onOpenChange: (open: boolean) => void;
	readonly title: string;
	readonly description?: string | undefined;
	/** `wide` for a document: the employer's page, a prepared résumé. */
	readonly size?: "wide" | undefined;
	/** The sheet's own buttons in place of Done: Cancel (a `SheetClose`) and its action. */
	readonly actions?: ReactNode;
	readonly children: ReactNode;
};

/** Closes the sheet it sits in. For a Cancel beside a sheet's own action. */
export const SheetClose = Dialog.Close;

/**
 * A macOS sheet: a titled panel over the window, dismissed with Escape, a click outside, or
 * Done. Radix supplies the focus trap and the accessible name; this supplies the look.
 */
export function Sheet({ open, onOpenChange, title, description, size, actions, children }: SheetProps) {
	return (
		<Dialog.Root open={open} onOpenChange={onOpenChange}>
			<Dialog.Portal>
				<Dialog.Overlay className="sheet-overlay" />
				<Dialog.Content className="sheet" data-size={size} aria-describedby={description === undefined ? undefined : "sheet-description"}>
					<div>
						<Dialog.Title className="sheet-title">{title}</Dialog.Title>
						{description === undefined ? null : (
							<Dialog.Description id="sheet-description" className="sheet-description">
								{description}
							</Dialog.Description>
						)}
					</div>
					{children}
					<div className="sheet-actions">
						{actions ?? <Dialog.Close className="button">Done</Dialog.Close>}
					</div>
				</Dialog.Content>
			</Dialog.Portal>
		</Dialog.Root>
	);
}
