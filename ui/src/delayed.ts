import { useEffect, useState } from "react";

/** How long something must keep loading before a screen says so. A quick reload says nothing. */
export const SLOW_MS = 400;

/**
 * True once `flag` has stayed true for `ms`, and false the moment it goes false.
 *
 * The job list reloads after every run and every `r`; most reloads answer in a blink, and a
 * screen that swapped to "Updating your job list…" for each one would flicker. A rebuild of
 * the view holds the read open for seconds, and that is when the state is worth showing.
 */
export function useDelayed(flag: boolean, ms: number = SLOW_MS): boolean {
	const [late, setLate] = useState(false);
	useEffect(() => {
		if (!flag) {
			setLate(false);
			return;
		}
		const timer = setTimeout(() => setLate(true), ms);
		return () => clearTimeout(timer);
	}, [flag, ms]);
	return flag && late;
}
