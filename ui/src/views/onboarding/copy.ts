/**
 * Every sentence the setup flow and the three first-run states say, in one place.
 *
 * The words are the Figma v6 boards' (ui/DESIGN.md, "Setup and first run"), so a copy change
 * is made here and on the board together. Identifiers still become English only in
 * `src/labels.ts`; nothing here names a runtime state, a board or a rule.
 */

export const SETUP_WINDOW_TITLE = "Set Up Venator";

export const STEP_COUNT = 3;

export function stepCaption(index: number): string {
	return `Step ${String(index)} of ${String(STEP_COUNT)}`;
}

export const SETUP = {
	skip: "Skip for Now",
	back: "Back",
	continue: "Continue",
	findJobs: "Find Jobs",
	saving: "Saving…",
} as const;

export const CONNECT = {
	title: "Connect an assistant",
	lede: "Venator uses it to read your résumé and help you apply.",
	claudeButton: "Use Claude",
	claudeLine: "Claude Code, signed in with your Claude plan.",
	chatgptButton: "Use ChatGPT",
	chatgptLine: "Codex, signed in with your ChatGPT plan.",
	checking: "Checking…",
	howToInstall: "How to install",
	jevKey: "Jev key",
	optional: "Optional",
	keySaved: "Saved in Keychain",
	keyPlaceholder: "Paste your TypeSafe key",
	keyReplacePlaceholder: "Paste a new key to replace the saved one",
	saveKey: "Save Key",
	keyCaption: "Jev sorts new jobs into look first, review and skip. The key stays in this Mac’s Keychain.",
	getKey: "Get a key",
	about: "About this",
	aboutLines: [
		"Venator runs the assistant on this Mac and reads its answers. It never sees your password.",
		"Checking a connection uses a few words of your plan.",
		"If your assistant stops working, Venator stops and tells you. It never switches to the other one.",
		"Venator isn’t made by, or partnered with, Anthropic or OpenAI.",
	],
	cannotCheck: "This copy of Venator can’t check an assistant.",
} as const;

export const RESUME = {
	title: "Add your résumé",
	lede: "Venator reads it and fills in your details. You check each one.",
	drop: "Drop your résumé PDF here",
	chooseFile: "Choose File…",
	pasteText: "Paste Text…",
	replace: "Replace…",
	readWith: "Read it with",
	thisMac: "This Mac, contact details only",
	reading: "Reading your résumé…",
	pastedName: "Pasted text",
	readOnThisMac: "Read on this Mac",
	readThisMacInstead: "Read It on This Mac",
	pasteTitle: "Paste your résumé",
	pasteDescription: "Venator reads your contact details from the text, on this Mac, for free.",
	pasteRead: "Read Text",
	cancel: "Cancel",
	looksRight: "Looks right",
	contact: "Contact",
	history: "Experience and education",
	skills: "Skills",
	editSkills: "Edit skills",
	addFirst: "Add a résumé first",
	onlyTicked: "Only what you tick goes into your Profile.",
	notOnePdf: "Choose one PDF.",
} as const;

/** What the paid reading costs, said before the file is chosen. */
export function wholeResumeOption(assistant: string): string {
	return `${assistant}, whole résumé`;
}

export function wholeResumeCaption(assistant: string): string {
	return `${assistant} reads every section, using one request of your plan. Choose This Mac to read only your contact details, for free.`;
}

export const THIS_MAC_CAPTION = "This Mac reads only your contact details, for free. Connect an assistant in step 1 to read every section.";

export function readByLine(assistant: string | null, pages: number | null): string {
	const who = assistant === null ? RESUME.readOnThisMac : `Read by ${assistant}`;
	if (pages === null) return who;
	return `${who} · ${String(pages)} ${pages === 1 ? "page" : "pages"}`;
}

export function missingContactNote(names: readonly string[]): string {
	return `Add your ${names.join(", ").toLowerCase()} to continue`;
}

export const CONFIRM_CONTACT = "Tick Looks right under Contact to continue";

export const TARGETING = {
	title: "What jobs should Venator find?",
	lede: "It checks these employers’ job boards each time you refresh.",
	jobs: "Jobs",
	titles: "Job titles",
	addTitle: "Add a title",
	level: "Level",
	places: "Places",
	addPlace: "Add a place",
	remote: "Remote jobs",
	employers: "Employers",
	search: "Paste an employer’s careers page",
	lookUp: "Look Up",
	lookingUp: "Looking at that page…",
	nothingFound: "That page names no job board Venator can check.",
	namePlaceholder: "Employer name",
	add: "Add",
	ready: "Ready",
	notChecked: "Didn’t answer",
	unnamed: "Unnamed board",
	addEmployerFirst: "Add an employer first",
} as const;

export function addedCount(count: number): string {
	return `${String(count)} added`;
}

export function removeEmployer(name: string): string {
	return `Remove ${name}`;
}

export function boardLine(source: string, confirmed: boolean, jobs: number, complete: boolean): string {
	if (!confirmed) return `${source} job board · didn’t answer just now`;
	return `${source} job board · ${complete ? "" : "at least "}${String(jobs)} ${jobs === 1 ? "job" : "jobs"}`;
}

export function titleHintNote(hint: string): string {
	return `The page calls itself “${hint}”. Type the employer’s name as you say it.`;
}

export const REPLACE = {
	exists: "You already have a Profile. Finishing setup replaces it.",
	replace: "Replace It",
} as const;

export const SAVED_SHEET = {
	title: "Your Profile is saved",
	description: "A few things are worth knowing:",
} as const;

export const FIRST_RUN = {
	noJobs: "No jobs yet",
	noJobsSetUp: "Set up Venator to choose which jobs to find.",
	setUp: "Set Up…",
	noEmployers: "Add the employers whose job boards Venator should check.",
	addEmployers: "Add Employers…",
	checking: "Venator is checking your employers’ job boards. Jobs appear here when it’s done.",
	refresh: "Refresh",
	updatingTitle: "Updating your job list…",
	updatingBody: "Your jobs will be back in a moment.",
	updatingToolbar: "Updating…",
	updatingSidebar: "Updating your job list",
	keyTitle: "Jev needs a key",
	addKey: "Add Key…",
	getKey: "Get a Key",
	keySheetTitle: "Add your Jev key",
	keySheetDescription: "The key stays in this Mac’s Keychain. Venator passes it only to Jev.",
	employersSheetTitle: "Add employers",
	employersSheetDescription: "Venator checks these employers’ job boards each time you refresh.",
	register: "Add to Profile",
} as const;

export function refreshBody(employers: number): string {
	return `Refresh to find jobs from your ${String(employers)} ${employers === 1 ? "employer" : "employers"}.`;
}

export function keyBody(waiting: number): string {
	return `Add your TypeSafe key and Jev sorts ${waiting === 1 ? "this job" : `these ${String(waiting)} jobs`} into look first, review and skip. Until then they wait here.`;
}

export const LINKS = {
	typesafeKeys: "https://console.typesafe.ai/keys",
	claudeInstall: "https://code.claude.com/docs/en/setup",
	codexInstall: "https://learn.chatgpt.com/docs/codex/cli",
} as const;

/** The Profile screen: an existing Profile as fields, and one Save. */
export const PROFILE = {
	title: "Profile",
	loading: "Opening your Profile…",
	save: "Save",
	saving: "Saving…",
	saved: "Profile saved.",
	unchanged: "Nothing to save yet",
	choose: "Choose a Profile",
	noProfile: "No Profile is stored in this Install yet.",
	chooseFirst: "Choose a Profile to see it.",
	contact: "Contact",
	history: "Experience and education",
	addExperience: "Add Experience…",
	addEducation: "Add Education…",
	skills: "Skills and tools",
	addSkills: "Add a heading…",
	remove: "Remove",
	done: "Done",
	jobs: "Jobs",
	levelAsWritten: "As written in the Profile",
	remoteUnset: "Not stated",
	filters: "Hard Filters",
	filtersLine: "Each one you turn on skips jobs before you see them. A job it can’t read passes.",
	education: "Education",
	holds: "Highest degree",
	inProgress: "In progress",
	degreeUnset: "Not stated",
	yearsKill: "Years asked for",
	yearsUnset: "Any",
	yearsLine: "A job asking for more years than this is skipped. Any never skips on years.",
	rolesToSkip: "Functions to skip",
	addRole: "Add a word",
	answers: "Application answers",
	answersLine: "Quoted back exactly when a form asks. Blank stays blank: nothing is guessed for you.",
	status: "Work status",
	sponsorship: "Needs sponsorship",
	authorized: "Authorized to work",
	authorizationLine: "A sponsorship question is answered for you only when this says Yes; No and Not answered both leave it to you. Authorized to work is answered as given, and Not answered leaves it to you.",
	screening: "Screening",
	eeo: "Voluntary self-identification",
	eeoLine: "Optional on every form. Blank leaves the question for you to answer by hand.",
	install: "This Mac",
	installLine: "Saved the moment you change it, apart from the Profile.",
	assistant: "Assistant for documents",
	notOpened: "This Profile could not be opened.",
} as const;
