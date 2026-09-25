import { openUrl } from "@tauri-apps/plugin-opener";
import type { ReactNode } from "react";

import { isDesktop } from "../platform.ts";

type ExternalLinkProps = {
	readonly href: string;
	readonly className: string;
	/** The kit's large capsule, when the link is drawn as a button. */
	readonly size?: "large" | undefined;
	readonly title: string;
	readonly children: ReactNode;
};

/**
 * An employer posting belongs in the owner's real browser, never in this window — the
 * dashboard is a read-only view of the pipeline and has no business hosting an apply form.
 * In a webview `target="_blank"` is inert, so the desktop build hands the URL to the shell.
 */
export function ExternalLink({ href, className, size, title, children }: ExternalLinkProps) {
	if (!isDesktop) {
		return (
			<a className={className} data-size={size} href={href} target="_blank" rel="noreferrer" title={title}>
				{children}
			</a>
		);
	}
	return (
		<button type="button" className={className} data-size={size} title={title} onClick={() => void openUrl(href)}>
			{children}
		</button>
	);
}
