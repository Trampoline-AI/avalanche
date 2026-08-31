import "@trampoline-ai/operator-ui/styles.css";
import { GrpcWebOperatorApi } from "@trampoline-ai/operator-ui";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { LocalOperatorShell } from "./local/LocalOperatorShell";
import "./local.css";

const root = document.getElementById("root");
if (!root) throw new Error("Operator UI root element is missing");
const operatorPort = document.querySelector<HTMLMetaElement>(
  "meta[data-avalanche-operator-port]",
);
if (operatorPort === null) throw new Error("Operator port metadata is missing");

createRoot(root).render(
  <StrictMode>
    <LocalOperatorShell api={new GrpcWebOperatorApi()} operatorPort={operatorPort.content} />
  </StrictMode>,
);
