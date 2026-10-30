import { CodeXml } from "lucide-react";

/** Leaves a run view for the current definition of the same node. */
export function OpenDefinitionButton({
  definitionLabel,
  onOpenDefinition,
}: {
  definitionLabel: string;
  onOpenDefinition: () => void;
}) {
  return (
    <button
      type="button"
      className="open-definition mt-3 inline-flex cursor-pointer items-center gap-1.5 rounded border border-line bg-panel px-2 py-1 text-[11px] text-secondary hover:border-secondary hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
      onClick={onOpenDefinition}
      title="Code and interface reflect the current deployment and may differ from what ran."
    >
      <CodeXml aria-hidden="true" className="size-3.5" strokeWidth={1.8} />
      View {definitionLabel.toLowerCase()}
    </button>
  );
}
