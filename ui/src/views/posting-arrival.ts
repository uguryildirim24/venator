/** The interval matches --motion-base. Rapid selections skip the page entry rather than queuing it. */
const BASE_MOTION_MS = 180;

export type PostingArrival = {
	readonly key: string | null;
	readonly at: number;
	readonly animate: boolean;
};

export function nextPostingArrival(previous: PostingArrival, key: string | null, at: number): PostingArrival {
	if (key === previous.key) return previous;
	return {
		key,
		at,
		animate: key !== null && previous.key !== null && at - previous.at >= BASE_MOTION_MS,
	};
}
