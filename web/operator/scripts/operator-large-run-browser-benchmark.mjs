import { spawn } from "node:child_process";
import { access, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, delimiter, dirname, join, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const RUN_COUNT = 10_000;
const DOM_ROW_LIMIT = 120;
const RUN_ROW_HEIGHT = 32;
const RENDER_BUDGET_MS = 3_000;
const INTERACTION_BUDGET_MS = 1_000;
const VITE_START_BUDGET_MS = 10_000;
const VITE_PROBE_BUDGET_MS = 500;
const CHROMIUM_PROCESS_BUDGET_MS = 15_000;

const operatorRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

async function findChromiumExecutable() {
  const pathDirectories = (process.env.PATH ?? "").split(delimiter).filter(Boolean);
  const candidates = [
    process.env.OPERATOR_CHROMIUM,
    process.env.CHROME_BIN,
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "/opt/google/chrome/chrome",
  ].filter(Boolean);

  for (const candidate of candidates) {
    const paths = candidate.includes("/")
      ? [resolve(candidate)]
      : pathDirectories.map((directory) => join(directory, candidate));
    for (const executable of paths) {
      try {
        await access(executable);
        return executable;
      } catch {
        // Try the next installed Chromium name.
      }
    }
  }
  throw new Error(
    "Chromium is required for web-bench; install Chromium or set OPERATOR_CHROMIUM or CHROME_BIN",
  );
}

async function waitForVite(url, server, stderr) {
  const deadline = performance.now() + VITE_START_BUDGET_MS;
  while (performance.now() < deadline) {
    if (server.exitCode !== null) {
      throw new Error(`Vite exited before serving the benchmark fixture: ${stderr()}`);
    }

    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      Math.min(VITE_PROBE_BUDGET_MS, deadline - performance.now()),
    );
    try {
      const response = await fetch(url, { signal: controller.signal });
      if (response.ok) return;
    } catch {
      // The socket may not be listening yet, or the readiness probe may have timed out.
    } finally {
      clearTimeout(timeout);
    }

    const remaining = deadline - performance.now();
    if (remaining > 0) {
      await new Promise((resolvePromise) => setTimeout(resolvePromise, Math.min(50, remaining)));
    }
  }

  const output = stderr().trim();
  throw new Error(
    `Vite did not start within ${VITE_START_BUDGET_MS}ms${output ? `:\n${output}` : ""}`,
  );
}

function delay(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

function childHasClosed(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function signalProcessTree(child, signal) {
  if (child.pid !== undefined && process.platform !== "win32") {
    try {
      process.kill(-child.pid, signal);
      return;
    } catch (error) {
      if (error?.code !== "ESRCH") throw error;
    }
  }
  if (!childHasClosed(child)) child.kill(signal);
}

async function waitForClose(closed, milliseconds) {
  return Promise.race([
    closed.then(() => true),
    delay(milliseconds).then(() => false),
  ]);
}

async function terminateChromium(browser, closed, cdp) {
  if (cdp && !childHasClosed(browser)) {
    try {
      await Promise.race([
        cdp.send("Browser.close", {}, performance.now() + 250).catch(() => {}),
        delay(250),
      ]);
    } catch {
      // Fall through to terminating the whole process group.
    }
  }
  cdp?.close();

  if (!(await waitForClose(closed, 250))) {
    signalProcessTree(browser, "SIGTERM");
    if (!(await waitForClose(closed, 500))) {
      signalProcessTree(browser, "SIGKILL");
    }
  }
  await closed;

  // Chrome can leave renderer descendants alive after its root exits. A detached
  // process group lets the harness terminate those descendants before removing
  // the profile they may still be using.
  if (browser.pid !== undefined && process.platform !== "win32") {
    try {
      process.kill(-browser.pid, "SIGKILL");
    } catch (error) {
      if (error?.code !== "ESRCH") throw error;
    }
  }
}

async function fetchJsonBeforeDeadline(url, deadline) {
  const remaining = deadline - performance.now();
  if (remaining <= 0) throw new Error("Chromium deadline expired");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), remaining);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) throw new Error(`DevTools endpoint returned ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

async function waitForDebuggerTarget(profilePath, browser, state, deadline) {
  let debuggingPort;
  let lastError;
  while (performance.now() < deadline) {
    if (state.launchError) throw state.launchError;
    if (childHasClosed(browser)) {
      throw new Error(
        `Chromium exited before exposing DevTools (code ${browser.exitCode}, signal ${browser.signalCode})` +
          (state.stderr ? `:\n${state.stderr}` : ""),
      );
    }

    if (debuggingPort === undefined) {
      try {
        const contents = await readFile(join(profilePath, "DevToolsActivePort"), "utf8");
        const candidate = Number(contents.split(/\r?\n/, 1)[0]);
        if (Number.isInteger(candidate) && candidate > 0) debuggingPort = candidate;
      } catch (error) {
        if (error?.code !== "ENOENT") lastError = error;
      }
    }

    if (debuggingPort !== undefined) {
      try {
        const targets = await fetchJsonBeforeDeadline(
          `http://127.0.0.1:${debuggingPort}/json`,
          deadline,
        );
        const page = targets.find(
          (target) => target.type === "page" && typeof target.webSocketDebuggerUrl === "string",
        );
        if (page) return page.webSocketDebuggerUrl;
      } catch (error) {
        lastError = error;
      }
    }
    await delay(Math.min(50, Math.max(0, deadline - performance.now())));
  }
  throw new Error(
    `Chromium did not expose a page DevTools target within ${CHROMIUM_PROCESS_BUDGET_MS}ms` +
      (lastError ? `: ${lastError instanceof Error ? lastError.message : String(lastError)}` : "") +
      (state.stderr ? `\n${state.stderr}` : ""),
  );
}

async function connectCdp(webSocketUrl, deadline) {
  if (typeof WebSocket !== "function") {
    throw new Error("This benchmark requires a Node.js runtime with global WebSocket support");
  }

  const socket = new WebSocket(webSocketUrl);
  const pending = new Map();
  const pageErrors = [];
  let nextId = 1;
  let closed = false;

  function rejectPending(error) {
    for (const request of pending.values()) {
      clearTimeout(request.timeout);
      request.reject(error);
    }
    pending.clear();
  }

  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(String(event.data));
    } catch {
      return;
    }
    if (message.method === "Runtime.exceptionThrown") {
      const details = message.params?.exceptionDetails;
      pageErrors.push(
        details?.exception?.description ?? details?.text ?? "Unknown page exception",
      );
      return;
    }
    if (message.id === undefined) return;
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    clearTimeout(request.timeout);
    if (message.error) {
      request.reject(
        new Error(
          `CDP ${request.method} failed (${message.error.code}): ${message.error.message}`,
        ),
      );
    } else {
      request.resolve(message.result);
    }
  });
  socket.addEventListener("close", () => {
    closed = true;
    rejectPending(new Error("Chromium DevTools connection closed"));
  });
  socket.addEventListener("error", () => {
    rejectPending(new Error("Chromium DevTools WebSocket failed"));
  });

  const remaining = deadline - performance.now();
  if (remaining <= 0) throw new Error("Chromium deadline expired before DevTools connected");
  await new Promise((resolvePromise, rejectPromise) => {
    const timeout = setTimeout(
      () => rejectPromise(new Error("Timed out connecting to Chromium DevTools")),
      remaining,
    );
    socket.addEventListener(
      "open",
      () => {
        clearTimeout(timeout);
        resolvePromise();
      },
      { once: true },
    );
    socket.addEventListener(
      "close",
      () => {
        clearTimeout(timeout);
        rejectPromise(new Error("Chromium DevTools connection closed before opening"));
      },
      { once: true },
    );
  });

  return {
    pageErrors,
    send(method, params = {}, commandDeadline = deadline) {
      if (closed || socket.readyState !== WebSocket.OPEN) {
        return Promise.reject(new Error(`Cannot send CDP ${method}: connection is closed`));
      }
      const commandRemaining = commandDeadline - performance.now();
      if (commandRemaining <= 0) {
        return Promise.reject(new Error(`Timed out before sending CDP ${method}`));
      }
      const id = nextId++;
      return new Promise((resolvePromise, rejectPromise) => {
        const timeout = setTimeout(() => {
          pending.delete(id);
          rejectPromise(new Error(`Timed out waiting for CDP ${method}`));
        }, commandRemaining);
        pending.set(id, {
          method,
          reject: rejectPromise,
          resolve: resolvePromise,
          timeout,
        });
        try {
          socket.send(JSON.stringify({ id, method, params }));
        } catch (error) {
          clearTimeout(timeout);
          pending.delete(id);
          rejectPromise(error);
        }
      });
    },
    close() {
      if (!closed) socket.close();
    },
  };
}

async function readBenchmarkResult(cdp, browser, state, deadline) {
  let latest = { bodyText: "", status: "" };
  while (performance.now() < deadline) {
    if (state.launchError) throw state.launchError;
    if (childHasClosed(browser)) {
      throw new Error(
        `Chromium exited during the benchmark (code ${browser.exitCode}, signal ${browser.signalCode})`,
      );
    }
    const evaluation = await cdp.send("Runtime.evaluate", {
      expression: `(() => {
        const body = document.body;
        return {
          status: body?.dataset.benchmarkStatus ?? "",
          bodyText: body?.innerText ?? "",
          domRows: body?.dataset.domRows ?? "",
          interactionMs: body?.dataset.interactionMs ?? "",
          renderMs: body?.dataset.renderMs ?? "",
          runCount: body?.dataset.runCount ?? "",
        };
      })()`,
      returnByValue: true,
    });
    if (evaluation.exceptionDetails) {
      throw new Error(
        evaluation.exceptionDetails.exception?.description ??
          evaluation.exceptionDetails.text ??
          "Benchmark result evaluation failed",
      );
    }
    latest = evaluation.result?.value ?? latest;
    if (latest.status === "pass") {
      if (cdp.pageErrors.length > 0) {
        throw new Error(`Real-browser benchmark failed:\n${cdp.pageErrors.join("\n")}`);
      }
      return latest;
    }
    if (latest.status === "fail") {
      throw new Error(
        `Real-browser benchmark failed:\n${latest.bodyText || cdp.pageErrors.join("\n") || "Unknown page failure"}`,
      );
    }
    await delay(Math.min(25, Math.max(0, deadline - performance.now())));
  }
  const details = [
    latest.bodyText,
    ...cdp.pageErrors,
    state.stderr && `Chromium stderr:\n${state.stderr}`,
  ].filter(Boolean);
  throw new Error(
    `Chromium exceeded the ${CHROMIUM_PROCESS_BUDGET_MS}ms process budget` +
      (details.length > 0 ? `\n${details.join("\n")}` : ""),
  );
}

async function runChromiumBenchmark(executable, url, profilePath) {
  const deadline = performance.now() + CHROMIUM_PROCESS_BUDGET_MS;
  const browser = spawn(
    executable,
    [
      "--headless=new",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--no-sandbox",
      "--no-first-run",
      "--no-default-browser-check",
      "--remote-allow-origins=*",
      "--remote-debugging-port=0",
      `--user-data-dir=${profilePath}`,
      "--window-size=1280,900",
      "about:blank",
    ],
    {
      detached: process.platform !== "win32",
      stdio: ["ignore", "ignore", "pipe"],
    },
  );
  browser.stderr.setEncoding("utf8");
  const state = { launchError: undefined, stderr: "" };
  browser.stderr.on("data", (chunk) => {
    state.stderr = (state.stderr + chunk).slice(-4_000);
  });
  browser.once("error", (error) => {
    state.launchError = error;
  });
  const closed = new Promise((resolvePromise) => {
    browser.once("close", (code, signal) => resolvePromise({ code, signal }));
  });

  let cdp;
  let result;
  let primaryError;
  try {
    const webSocketUrl = await waitForDebuggerTarget(profilePath, browser, state, deadline);
    cdp = await connectCdp(webSocketUrl, deadline);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    const navigation = await cdp.send("Page.navigate", { url });
    if (navigation.errorText) {
      throw new Error(
        `Chromium could not navigate to the benchmark fixture: ${navigation.errorText}`,
      );
    }
    result = await readBenchmarkResult(cdp, browser, state, deadline);
  } catch (error) {
    primaryError = error;
  } finally {
    try {
      await terminateChromium(browser, closed, cdp);
    } catch (error) {
      if (primaryError) {
        process.stderr.write(
          `Chromium cleanup also failed: ${error instanceof Error ? error.message : String(error)}\n`,
        );
      } else {
        primaryError = error;
      }
    }
  }

  if (primaryError) throw primaryError;
  return result;
}

function browserBenchmarkFixture() {
  return `
import React from "react";
import { createRoot } from "react-dom/client";
import { RunListPanel } from "/src/RunListPanel";
import "/src/styles.css";

const RUN_COUNT = ${RUN_COUNT};
const DOM_ROW_LIMIT = ${DOM_ROW_LIMIT};
const RUN_ROW_HEIGHT = ${RUN_ROW_HEIGHT};
const RENDER_BUDGET_MS = ${RENDER_BUDGET_MS};
const INTERACTION_BUDGET_MS = ${INTERACTION_BUDGET_MS};
const workflowId = "benchmark.py::large_run";
const summaries = Array.from({ length: RUN_COUNT }, (_, index) => {
  const runId = "run-" + index.toString().padStart(5, "0");
  return {
    runId,
    workflowId,
    workflowDisplayName: "Benchmark flow",
    status: index % 7 === 0 ? "failed" : "success",
    startedAt: index + 1,
    endedAt: index + 2,
    createdSequence: String(index + 1),
    revision: String(index + 1),
  };
});
const runs = Object.fromEntries(summaries.map((summary) => [summary.runId, summary]));

async function until<T extends Element>(
  find: () => T | undefined,
  budgetMs: number,
  message: string,
) {
  const startedAt = performance.now();
  while (performance.now() - startedAt <= budgetMs) {
    const found = find();
    if (found) return found;
    await new Promise<void>((resolvePromise) => requestAnimationFrame(() => resolvePromise()));
  }
  throw new Error(message);
}
function rows() {
  return Array.from(document.querySelectorAll<HTMLElement>(".run-list-row"));
}
function assertDomBound() {
  if (rows().length === 0 || rows().length > DOM_ROW_LIMIT) {
    throw new Error("virtualized run DOM exceeded " + DOM_ROW_LIMIT + ": " + rows().length);
  }
}

async function run() {
  const rootElement = document.getElementById("root");
  if (!rootElement) throw new Error("benchmark root missing");
  let selectedRunId = "";
  const renderStartedAt = performance.now();
  createRoot(rootElement).render(
    <RunListPanel
      workflowId={workflowId}
      runs={runs}
      onSelectRun={(runId) => {
        selectedRunId = runId;
      }}
    />,
  );
  const panel = await until(
    () => document.querySelector<HTMLElement>(".run-list-panel") ?? undefined,
    RENDER_BUDGET_MS,
    "Run list panel did not render",
  );
  const virtualList = await until(
    () => document.querySelector<HTMLElement>(".run-list-virtual") ?? undefined,
    RENDER_BUDGET_MS,
    "virtual run list did not render",
  );
  await until(
    () => Array.from(document.querySelectorAll<HTMLButtonElement>(".run-list-row"))
      .some((row) => row.textContent?.includes("run-09999")) ? virtualList : undefined,
    RENDER_BUDGET_MS,
    "production virtualizer did not emit the initial visible range",
  );
  const renderMs = performance.now() - renderStartedAt;
  if (renderMs > RENDER_BUDGET_MS) {
    throw new Error("initial render exceeded " + RENDER_BUDGET_MS + "ms: " + renderMs);
  }
  if (virtualList.getBoundingClientRect().height < RUN_COUNT * RUN_ROW_HEIGHT) {
    throw new Error("virtual list did not retain deterministic 10k-row geometry");
  }
  assertDomBound();
  const scrollElement = panel.querySelector<HTMLElement>(".run-list-scroll");
  if (!scrollElement) throw new Error("run list scroll element missing");

  const interactionStartedAt = performance.now();
  scrollElement.scrollTop = scrollElement.scrollHeight - scrollElement.clientHeight;
  scrollElement.dispatchEvent(new Event("scroll"));
  const oldestRun = await until(
    () => Array.from(document.querySelectorAll<HTMLButtonElement>(".run-list-row"))
      .find((button) => button.textContent?.includes("run-00000")),
    INTERACTION_BUDGET_MS,
    "scroll did not render the oldest run",
  );
  const interactionMs = performance.now() - interactionStartedAt;
  if (interactionMs > INTERACTION_BUDGET_MS) {
    throw new Error(
      "scroll interaction exceeded " + INTERACTION_BUDGET_MS + "ms: " + interactionMs,
    );
  }
  assertDomBound();
  oldestRun.click();
  if (selectedRunId !== "run-00000") throw new Error("oldest run interaction failed");

  document.body.dataset.benchmarkStatus = "pass";
  document.body.dataset.domRows = String(rows().length);
  document.body.dataset.interactionMs = interactionMs.toFixed(2);
  document.body.dataset.renderMs = renderMs.toFixed(2);
  document.body.dataset.runCount = String(RUN_COUNT);
}

run().catch((error) => {
  document.body.dataset.benchmarkStatus = "fail";
  document.body.textContent = error instanceof Error ? error.stack ?? error.message : String(error);
});
`;
}

function assertBrowserResult(result) {
  const domRows = Number(result.domRows);
  const interactionMs = Number(result.interactionMs);
  const renderMs = Number(result.renderMs);
  if (result.runCount !== String(RUN_COUNT)) {
    throw new Error(`Chromium result did not retain ${RUN_COUNT} Explorer runs`);
  }
  if (!(domRows > 0 && domRows <= DOM_ROW_LIMIT)) {
    throw new Error(`Chromium retained ${domRows} run rows; expected 1..${DOM_ROW_LIMIT}`);
  }
  if (!(interactionMs <= INTERACTION_BUDGET_MS)) {
    throw new Error(
      `Chromium interaction took ${interactionMs}ms; budget is ${INTERACTION_BUDGET_MS}ms`,
    );
  }
  if (!(renderMs <= RENDER_BUDGET_MS)) {
    throw new Error(`Chromium render took ${renderMs}ms; budget is ${RENDER_BUDGET_MS}ms`);
  }
  process.stdout.write(
    `Operator Chromium benchmark passed: ${RUN_COUNT} runs, ${domRows} DOM rows, ` +
      `${renderMs.toFixed(2)}ms render, ${interactionMs.toFixed(2)}ms interaction\n`,
  );
}

async function main() {
  const chromium = await findChromiumExecutable();
  const fixtureDirectory = await mkdtemp(join(operatorRoot, "operator-browser-benchmark-"));
  const chromiumProfile = await mkdtemp(join(tmpdir(), "avalanche-operator-chromium-"));
  let viteServer;
  let viteOutput = "";
  let primaryError;
  try {
    await writeFile(
      join(fixtureDirectory, "index.html"),
      `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <style>
      html, body, #root { height: 100%; margin: 0; }
      #root { width: 300px; }
      .run-list-panel { height: 222px; width: 300px; }
    </style>
    <title>Operator real virtualizer benchmark</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="./fixture.tsx"></script>
  </body>
</html>
`,
    );
    await writeFile(join(fixtureDirectory, "fixture.tsx"), browserBenchmarkFixture());
    await writeFile(
      join(fixtureDirectory, "vite.benchmark.config.mjs"),
      "export default { server: { hmr: false } };",
    );

    const port = 41_000 + (process.pid % 1_000);
    const fixtureUrl = `http://127.0.0.1:${port}/${basename(fixtureDirectory)}/index.html`;
    viteServer = spawn(
      process.execPath,
      [
        join(operatorRoot, "node_modules/vite/bin/vite.js"),
        "--host",
        "127.0.0.1",
        "--port",
        String(port),
        "--strictPort",
        "--config",
        join(fixtureDirectory, "vite.benchmark.config.mjs"),
      ],
      { cwd: operatorRoot, stdio: ["ignore", "pipe", "pipe"] },
    );
    viteServer.stdout.setEncoding("utf8");
    viteServer.stderr.setEncoding("utf8");
    viteServer.stdout.on("data", (chunk) => {
      viteOutput += chunk;
    });
    viteServer.stderr.on("data", (chunk) => {
      viteOutput += chunk;
    });
    await waitForVite(fixtureUrl, viteServer, () => viteOutput);
    assertBrowserResult(await runChromiumBenchmark(chromium, fixtureUrl, chromiumProfile));
  } catch (error) {
    primaryError = error;
  } finally {
    if (viteServer?.exitCode === null) viteServer.kill("SIGTERM");
    try {
      await Promise.all([
        rm(fixtureDirectory, { force: true, maxRetries: 10, recursive: true, retryDelay: 100 }),
        rm(chromiumProfile, { force: true, maxRetries: 10, recursive: true, retryDelay: 100 }),
      ]);
    } catch (error) {
      if (primaryError) {
        process.stderr.write(
          `Benchmark cleanup also failed: ${error instanceof Error ? error.message : String(error)}\n`,
        );
      } else {
        primaryError = error;
      }
    }
  }
  if (primaryError) throw primaryError;
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
  process.exitCode = 1;
});
