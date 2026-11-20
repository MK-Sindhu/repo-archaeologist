import "@fontsource-variable/archivo/wdth.css";
import "@fontsource-variable/literata";
import "@fontsource-variable/martian-mono/wdth.css";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/layout.css";
import "./styles/section.css";
import "./styles/pages.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
