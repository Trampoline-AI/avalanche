import "@xyflow/react/dist/style.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { GrpcWebOperatorApi } from "./api";
import { App } from "./App";
import "./tailwind.css";

const root = document.getElementById("root");
if (!root) throw new Error("Operator UI root element is missing");

createRoot(root).render(
  <StrictMode>
    <App api={new GrpcWebOperatorApi()} />
  </StrictMode>,
);
