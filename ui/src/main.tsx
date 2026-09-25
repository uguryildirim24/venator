import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app.tsx";
import "./styles.css";

const container = document.getElementById("root");
if (container === null) {
	throw new Error("index.html is missing its #root element.");
}

// Light and dark follow the system, in a browser and on the desktop alike (ui/DESIGN.md).
createRoot(container).render(
	<StrictMode>
		<App />
	</StrictMode>,
);
