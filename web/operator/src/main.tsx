import "@xyflow/react/dist/style.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { GrpcWebOperatorApi } from "./api";
import { LocalOperatorShell } from "./local/LocalOperatorShell";
import "./style.css";

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
