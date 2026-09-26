// Conservative US city/state table for state-free ATS labels. Multiple states remain unresolved.
// Keep this explicit: the location filter must not turn a shared city name into a guessed state.
export const US_CITIES = {
	"boston": ["MA"], "cambridge": ["MA"], "worcester": ["MA"], "brookline": ["MA"],
	"east boston": ["MA"], "south boston": ["MA"], "south san francisco": ["CA"],
	"san francisco": ["CA"], "los angeles": ["CA"], "san diego": ["CA"],
	"new york": ["NY"], "new york city": ["NY"], "tarrytown": ["NY"],
	"sleepy hollow": ["NY"], "chicago": ["IL"], "seattle": ["WA"],
	"indianapolis": ["IN"], "phoenix": ["AZ"], "philadelphia": ["PA"],
	"houston": ["TX"], "dallas": ["TX"], "austin": ["TX"],
	"gaithersburg": ["MD"], "princeton": ["NJ"], "redwood city": ["CA"],
	"durham": ["NC"], "burlington": ["MA", "NC", "VT"],
	"springfield": ["MA", "IL", "MO"], "portland": ["ME", "OR"],
} satisfies Record<string, readonly string[]>;
