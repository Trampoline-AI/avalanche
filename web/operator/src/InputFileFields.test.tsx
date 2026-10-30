import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import type { OperatorApi } from "./api";
import { FlowInfoMsg, type InputFileTarget, type StagedInputFile } from "./model";
import { RunControls } from "./RunControls";
import { workflow } from "./test/fixtures";
import { useInputDraft } from "./useInputDraft";
import { WorkflowInputInspector } from "./WorkflowInputInspector";

const fileDefinition = {
  type: "object",
  title: "File",
  properties: {
    kind: { const: "file", default: "file", type: "string" },
    name: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
  },
  required: ["ref", "size"],
};
const inputSchema = {
  title: "Documents",
  type: "object",
  $defs: { File: fileDefinition },
  properties: {
    document: { $ref: "#/$defs/File" },
    attachments: { type: "array", items: { $ref: "#/$defs/File" }, default: [] },
    note: { type: "string", default: "" },
  },
  required: ["document"],
};
const documents = FlowInfoMsg.create({
  ...workflow,
  cron: "0 9 * * *",
  inputSchemaJson: JSON.stringify(inputSchema),
  inputDeclarationJson: JSON.stringify({
    type_name: "Documents",
    json_schema: inputSchema,
    consumer_node_ids: ["fetch"],
  }),
});

function staged(name: string): StagedInputFile {
  return {
    name,
    mediaType: "text/plain",
    sizeBytes: 2048,
    sha256: "ab".repeat(32),
    objectUri: "",
    objectKey: `token/${name}`,
  };
}

type Stage = (file: File, target: InputFileTarget) => Promise<StagedInputFile>;

function Harness({
  workflow = documents,
  api,
  onStart,
  onSetScheduleEnabled,
}: {
  workflow?: FlowInfoMsg;
  api: Pick<OperatorApi, "stageInputFile">;
  onStart: Parameters<typeof RunControls>[0]["onStart"];
  onSetScheduleEnabled?: Parameters<typeof RunControls>[0]["onSetScheduleEnabled"];
}) {
  const draft = useInputDraft(workflow, api);
  return (
    <>
      <RunControls
        workflow={workflow}
        onStart={onStart}
        onCancel={vi.fn()}
        onSetScheduleEnabled={onSetScheduleEnabled}
        inputDraft={draft}
      />
      <WorkflowInputInspector workflow={workflow} inputDraft={draft} onClose={vi.fn()} />
    </>
  );
}

function renderFields(stageInputFile?: Stage, workflow?: FlowInfoMsg) {
  const onStart = vi.fn(async () => "run-1");
  const onSetScheduleEnabled = vi.fn(async () => undefined);
  render(
    <Harness
      workflow={workflow}
      api={{ stageInputFile }}
      onStart={onStart}
      onSetScheduleEnabled={onSetScheduleEnabled}
    />,
  );
  return { onStart, onSetScheduleEnabled };
}

const text = (name: string) => new File(["hi"], name, { type: "text/plain" });

it("stages a single file when it is picked and runs with it", async () => {
  const stageInputFile = vi.fn<Stage>(async (file) => staged(file.name));
  const { onStart } = renderFields(stageInputFile);

  await userEvent.upload(screen.getByLabelText("document file"), text("report.txt"));

  expect(stageInputFile).toHaveBeenCalledWith(expect.any(File), {
    workflow: documents.workflowId,
    field: "document",
  });
  const chosen = await screen.findByRole("list", { name: "document file chosen" });
  await waitFor(() => expect(chosen).toHaveAttribute("data-file-status", "staged"));
  expect(within(chosen).getAllByRole("listitem")).toHaveLength(1);

  await userEvent.click(screen.getByRole("button", { name: "Run" }));

  await waitFor(() =>
    expect(onStart).toHaveBeenCalledWith(documents.workflowId, {}, [
      { ...staged("report.txt"), fieldName: "document" },
    ]),
  );
});

it("stages every file picked for a list field in order", async () => {
  const stageInputFile = vi.fn<Stage>(async (file) => staged(file.name));
  const { onStart, onSetScheduleEnabled } = renderFields(stageInputFile);
  const picker = screen.getByLabelText("attachments files");
  expect(picker).toHaveAttribute("multiple");

  await userEvent.upload(screen.getByLabelText("document file"), text("main.txt"));
  await userEvent.upload(picker, [text("a.txt"), text("b.txt")]);

  expect(stageInputFile).toHaveBeenCalledTimes(3);
  const chosen = await screen.findByRole("list", { name: "attachments files chosen" });
  await waitFor(() => expect(within(chosen).getAllByRole("listitem")).toHaveLength(2));
  await waitFor(() => expect(chosen).toHaveAttribute("data-file-status", "staged"));

  const expectedFiles = [
    { ...staged("main.txt"), fieldName: "document" },
    { ...staged("a.txt"), fieldName: "attachments" },
    { ...staged("b.txt"), fieldName: "attachments" },
  ];
  await userEvent.click(screen.getByRole("button", { name: "Run" }));
  await waitFor(() =>
    expect(onStart).toHaveBeenCalledWith(
      documents.workflowId,
      { attachments: [] },
      expectedFiles,
    ),
  );
  await userEvent.click(screen.getByRole("switch", { name: "Run on schedule" }));
  await waitFor(() =>
    expect(onSetScheduleEnabled).toHaveBeenCalledWith(
      documents.workflowId,
      true,
      { attachments: [] },
      expectedFiles,
    ),
  );
});

it("replaces an earlier pick and sends the files with Use for schedule", async () => {
  const stageInputFile = vi.fn<Stage>(async (file) => staged(file.name));
  const scheduled = FlowInfoMsg.create({ ...documents, scheduleEnabled: true });
  const { onSetScheduleEnabled } = renderFields(stageInputFile, scheduled);
  const picker = screen.getByLabelText("attachments files");

  await userEvent.upload(screen.getByLabelText("document file"), text("main.txt"));
  await userEvent.upload(picker, [text("old-a.txt"), text("old-b.txt")]);
  await userEvent.upload(picker, text("new.txt"));
  const chosen = await screen.findByRole("list", { name: "attachments files chosen" });
  await waitFor(() => expect(within(chosen).getAllByRole("listitem")).toHaveLength(1));
  await waitFor(() => expect(chosen).toHaveAttribute("data-file-status", "staged"));

  await userEvent.click(screen.getByRole("button", { name: "Use for schedule" }));

  await waitFor(() =>
    expect(onSetScheduleEnabled).toHaveBeenCalledWith(
      scheduled.workflowId,
      true,
      { attachments: [] },
      [
        { ...staged("main.txt"), fieldName: "document" },
        { ...staged("new.txt"), fieldName: "attachments" },
      ],
    ),
  );
});

it("refuses to run while a picked file is still uploading", async () => {
  const stageInputFile = vi.fn<Stage>(() => new Promise(() => undefined));
  const { onStart } = renderFields(stageInputFile);

  await userEvent.upload(screen.getByLabelText("document file"), text("slow.txt"));
  expect(await screen.findByRole("status")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Run" }));

  expect(await screen.findByRole("alert")).toBeVisible();
  expect(onStart).not.toHaveBeenCalled();
});

it("shows an upload the host refused and clears the pick", async () => {
  const stageInputFile = vi.fn<Stage>(async () => {
    throw new Error("upload refused");
  });
  renderFields(stageInputFile);

  await userEvent.upload(screen.getByLabelText("document file"), text("bad.txt"));
  expect(await screen.findByRole("alert")).toBeVisible();

  await userEvent.click(screen.getByRole("button", { name: "Clear document" }));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("list", { name: "document file chosen" })).toBeNull();
});

it("keeps file fields read-only when the host cannot stage files", async () => {
  const { onStart } = renderFields();

  expect(screen.getByLabelText("document file")).toBeDisabled();
  expect(screen.getByLabelText("attachments files")).toBeDisabled();
  expect(screen.getByRole("textbox", { name: "note value" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "Clear document" })).toBeNull();

  await userEvent.click(screen.getByRole("button", { name: "Run" }));
  // The draft still holds the defaults, so the operator fills them in.
  await waitFor(() =>
    expect(onStart).toHaveBeenCalledWith(documents.workflowId, undefined, undefined),
  );
});
