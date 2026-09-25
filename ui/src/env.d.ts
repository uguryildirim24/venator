/// <reference types="vite/client" />

/** The only build-time knob: where the dashboard looks for its API. */
interface ImportMetaEnv {
	readonly VITE_API_BASE?: string;
}

interface ImportMeta {
	readonly env: ImportMetaEnv;
}
