import type { PostingDetail } from "../../shared/contracts.ts";
import { useContacts } from "../api.ts";
import type { ApplicationControl } from "../application.ts";
import { formatDay } from "../format.ts";
import { contactRouteLabel } from "../labels.ts";
import { ExternalLink } from "./external-link.tsx";

export function ContactMargin({ detail, control }: { readonly detail: PostingDetail; readonly control: ApplicationControl }) {
	const response = useContacts(detail.posting.key, detail.trackEvents.length);
	if (response.status !== "ready") return null;
	return <>{response.value.contacts.map((contact) => <div className="note" key={contact.id}>
		<span className="note-title">{contact.person}</span>
		{contact.title ? <span className="note-subtitle">{contact.title}</span> : null}
		{contact.conversationAngle ? <span className="note-subtitle">{contact.conversationAngle}</span> : null}
		{contact.href === null ? null : <ExternalLink className="note-action" href={contact.href} title={`Contact ${contact.person}`}>{contactRouteLabel(contact.href, contact.contactRoute)}</ExternalLink>}
		{contact.reachedOutAt === null
			? <button type="button" className="note-action" disabled={control.busy !== null} onClick={() => control.run("outreach", { id: contact.id })}>Reached out</button>
			: <><span className="note-subtitle">Reached out {formatDay(contact.reachedOutAt)}</span>
				<button type="button" className="note-action" disabled={control.busy !== null} onClick={() => control.run("outreach_undo", { id: contact.id })}>Undo</button></>}
	</div>)}</>;
}
