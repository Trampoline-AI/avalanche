import { useId, useMemo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import type {
  ClassifierDeclaration,
  ClassifierJsonSchema,
  ClassifierSchema,
  ClassifierStepOutput,
} from "./classifier";
import { ValueView } from "./ValueView";

type SchemaChild = {
  label: string;
  path: string;
  schema: ClassifierSchema;
  required?: boolean;
};

function schemaChildren(
  schema: ClassifierJsonSchema,
  includeDefinitions = false,
): SchemaChild[] {
  const children: SchemaChild[] = [];
  const maps: [string, Record<string, ClassifierSchema> | undefined][] = [
    ["properties", schema.properties],
    ["patternProperties", schema.patternProperties],
    ["dependentSchemas", schema.dependentSchemas],
  ];
  if (includeDefinitions) {
    maps.push(["$defs", schema.$defs], ["definitions", schema.definitions]);
  }
  for (const [keyword, schemas] of maps) {
    for (const [name, value] of Object.entries(schemas ?? {})) {
      children.push({
        label: keyword === "properties" ? name : `${keyword}: ${name}`,
        path: `${keyword}/${name.replace(/~/g, "~0").replace(/\//g, "~1")}`,
        schema: value,
        required:
          keyword === "properties" ? (schema.required?.includes(name) ?? false) : undefined,
      });
    }
  }
  const arrays: [string, ClassifierSchema[] | undefined][] = [
    ["anyOf", schema.anyOf],
    ["oneOf", schema.oneOf],
    ["allOf", schema.allOf],
    ["prefixItems", schema.prefixItems],
  ];
  for (const [keyword, schemas] of arrays) {
    schemas?.forEach((value, index) =>
      children.push({
        label: `${keyword === "prefixItems" ? "Item" : keyword} ${index + 1}`,
        path: `${keyword}/${index}`,
        schema: value,
      }),
    );
  }
  if (Array.isArray(schema.items)) {
    schema.items.forEach((value, index) =>
      children.push({
        label: `Item ${index + 1}`,
        path: `items/${index}`,
        schema: value,
      }),
    );
  } else if (schema.items !== undefined) {
    children.push({ label: "Items", path: "items", schema: schema.items });
  }
  const nested: [string, string, ClassifierSchema | undefined][] = [
    ["additionalProperties", "Additional properties", schema.additionalProperties],
    ["additionalItems", "Additional items", schema.additionalItems],
    ["unevaluatedProperties", "Unevaluated properties", schema.unevaluatedProperties],
    ["unevaluatedItems", "Unevaluated items", schema.unevaluatedItems],
    ["propertyNames", "Property names", schema.propertyNames],
    ["contains", "Contains", schema.contains],
    ["not", "Not", schema.not],
    ["if", "If", schema.if],
    ["then", "Then", schema.then],
    ["else", "Else", schema.else],
  ];
  for (const [path, label, value] of nested) {
    if (value !== undefined)
      children.push({
        label,
        path,
        schema: value,
      });
  }
  return children;
}

function schemaIndex(root: ClassifierJsonSchema): ReadonlyMap<string, ClassifierSchema> {
  const index = new Map<string, ClassifierSchema>();
  function visit(schema: ClassifierSchema, pointer: string) {
    index.set(pointer, schema);
    if (typeof schema !== "boolean") {
      for (const child of schemaChildren(schema, true))
        visit(child.schema, `${pointer}/${child.path}`);
    }
  }
  visit(root, "#");
  return index;
}

function schemaType(schema: ClassifierSchema): string {
  if (typeof schema === "boolean") return schema ? "Any JSON" : "No values allowed";
  if (schema.type) return Array.isArray(schema.type) ? schema.type.join(" | ") : schema.type;
  if (schema.$ref !== undefined)
    return (
      schema.$ref.split("/").at(-1)?.replace(/~1/g, "/").replace(/~0/g, "~") || "Reference"
    );
  if (schema.anyOf) return "Union";
  if (schema.oneOf) return "Exactly one variant";
  if (schema.allOf) return "All constraints";
  if (schema.properties) return "object";
  if (schema.items !== undefined || schema.prefixItems) return "array";
  if (schema.enum) return "enum";
  return "Any JSON";
}

const STRUCTURAL_KEYWORDS: Record<string, true> = {
  type: true,
  title: true,
  description: true,
  $ref: true,
  required: true,
  properties: true,
  patternProperties: true,
  dependentSchemas: true,
  $defs: true,
  definitions: true,
  items: true,
  prefixItems: true,
  anyOf: true,
  oneOf: true,
  allOf: true,
  additionalProperties: true,
  additionalItems: true,
  unevaluatedProperties: true,
  unevaluatedItems: true,
  propertyNames: true,
  contains: true,
  not: true,
  if: true,
  then: true,
  else: true,
};

function SchemaNode({
  label,
  schema,
  index,
  ancestors,
  required,
  typeName,
}: {
  label: string;
  schema: ClassifierSchema;
  index: ReadonlyMap<string, ClassifierSchema>;
  ancestors: readonly ClassifierSchema[];
  required?: boolean;
  typeName?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const detailsId = useId();
  const object = typeof schema === "boolean" ? undefined : schema;
  const children = object ? schemaChildren(object) : [];
  const annotations = object
    ? Object.entries(object).filter(
        ([key]) =>
          !Object.hasOwn(STRUCTURAL_KEYWORDS, key) ||
          (key === "required" &&
            object.required?.some((name) => !Object.hasOwn(object.properties ?? {}, name))),
      )
    : [];
  const reference = object?.$ref;
  let referenced: ClassifierSchema | undefined;
  if (reference === "" || reference?.startsWith("#")) {
    try {
      referenced = index.get(reference === "" ? "#" : decodeURIComponent(reference));
    } catch {
      // An invalid URI fragment is displayed as unresolved, never fetched.
    }
  }
  const recursive =
    referenced !== undefined && (referenced === schema || ancestors.includes(referenced));
  // Pydantic stores nested model bodies in $defs. Show their fields directly
  // under the referencing field, not as a second "Reference target" model.
  if (
    object &&
    referenced !== undefined &&
    !recursive &&
    Object.keys(object).every((key) => ["$ref", "$defs", "definitions"].includes(key))
  ) {
    return (
      <SchemaNode
        label={label}
        schema={referenced}
        index={index}
        ancestors={[...ancestors, schema]}
        required={required}
        typeName={typeName}
      />
    );
  }
  const expandable = Boolean(
    object?.description || reference !== undefined || children.length || annotations.length,
  );
  const type = schemaType(schema);
  return (
    <div
      role="group"
      aria-label={`${label} schema`}
      className="min-w-0 text-[11px] [overflow-wrap:anywhere]"
    >
      <div className="flex min-w-0 items-start gap-1.5">
        {expandable ? (
          <button
            type="button"
            aria-label={`${expanded ? "Collapse" : "Expand"} ${label} schema`}
            aria-expanded={expanded}
            aria-controls={detailsId}
            onClick={() => setExpanded(!expanded)}
            className="-m-0.5 shrink-0 cursor-pointer rounded border-0 bg-transparent p-0.5 text-muted hover:bg-canvas hover:text-classifier focus-visible:outline-2 focus-visible:outline-classifier"
          >
            {expanded ? (
              <ChevronDown className="size-3.5" />
            ) : (
              <ChevronRight className="size-3.5" />
            )}
          </button>
        ) : (
          <span className="w-3.5 shrink-0" aria-hidden="true" />
        )}
        <div className="min-w-0 flex-1">
          <strong className="font-semibold">{label}</strong>
          <code className="ml-2 font-mono text-[10px] text-muted">
            {typeName || object?.title || type}
            {(typeName || object?.title) && type !== (typeName || object?.title)
              ? ` · ${type}`
              : ""}
          </code>
        </div>
        {required !== undefined && (
          <span className="text-[9px] text-muted">{required ? "Required" : "Optional"}</span>
        )}
      </div>
      {expanded && (
        <div
          id={detailsId}
          className="mt-2 ml-1.5 grid min-w-0 gap-2 border-l border-line pl-3"
        >
          {object?.description && (
            <p className="m-0 whitespace-pre-wrap text-secondary">{object.description}</p>
          )}
          {annotations.map(([key, value]) => (
            <div key={key} className="min-w-0" role="group" aria-label={`${label} ${key}`}>
              <span className="font-mono text-[9px] text-muted">{key}</span>
              <ValueView value={value} jsonOnly />
            </div>
          ))}
          {reference !== undefined && (
            <div className="min-w-0">
              <p className="m-0 font-mono text-[10px] text-muted">{reference}</p>
              {referenced === undefined ? (
                <p className="mt-1 mb-0 text-muted">
                  {reference.startsWith("#")
                    ? "Unresolved local reference."
                    : "External reference; not fetched."}
                </p>
              ) : recursive ? (
                <p className="mt-1 mb-0 text-muted">
                  Recursive reference; schema already shown above.
                </p>
              ) : (
                <div className="mt-2">
                  <SchemaNode
                    label="Reference target"
                    schema={referenced}
                    index={index}
                    ancestors={[...ancestors, schema]}
                  />
                </div>
              )}
            </div>
          )}
          {children.map((child) => (
            <SchemaNode
              key={child.path}
              label={child.label}
              schema={child.schema}
              required={child.required}
              index={index}
              ancestors={[...ancestors, schema]}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function DeclaredSchema({
  label,
  schema,
  required,
  typeName,
}: {
  label: string;
  schema: ClassifierJsonSchema;
  required?: boolean;
  typeName?: string;
}) {
  const index = useMemo(() => schemaIndex(schema), [schema]);
  return (
    <SchemaNode
      label={label}
      schema={schema}
      index={index}
      ancestors={[]}
      required={required}
      typeName={typeName}
    />
  );
}

function SignatureType({
  label,
  definition,
  required,
}: {
  label: string;
  definition: ClassifierStepOutput;
  required?: boolean;
}) {
  if (definition.json_schema !== null) {
    return (
      <DeclaredSchema
        label={label}
        schema={definition.json_schema}
        typeName={definition.type_name}
        required={required}
      />
    );
  }
  return (
    <div className="min-w-0 pl-5 text-[11px] [overflow-wrap:anywhere]">
      <div className="flex gap-2">
        <strong className="font-semibold">{label}</strong>
        <code className="flex-1 font-mono text-[10px] text-muted">{definition.type_name}</code>
        {required !== undefined && (
          <span className="text-[9px] text-muted">{required ? "Required" : "Optional"}</span>
        )}
      </div>
      <p className="mt-1 mb-0 text-[10px] text-muted">
        {definition.type_name === "Unspecified"
          ? "No annotation declared."
          : "JSON schema unavailable for this annotation."}
      </p>
    </div>
  );
}

export function ClassifierStepSchemas({ declaration }: { declaration: ClassifierDeclaration }) {
  return (
    <div className="grid min-w-0 gap-4">
      <section aria-label="Step inputs" className="min-w-0">
        <h4 className="mt-0 mb-2 font-mono text-[9px] tracking-[.08em] text-muted uppercase">
          Inputs
        </h4>
        <div className="grid min-w-0 gap-2">
          {declaration.step_inputs.length ? (
            declaration.step_inputs.map((input) => (
              <SignatureType
                key={input.name}
                label={input.name}
                definition={input}
                required={input.required}
              />
            ))
          ) : (
            <p className="m-0 text-[11px] text-muted">No step inputs.</p>
          )}
        </div>
      </section>
      <section aria-label="Step output" className="min-w-0">
        <h4 className="mt-0 mb-2 font-mono text-[9px] tracking-[.08em] text-muted uppercase">
          Output
        </h4>
        <SignatureType label="Return" definition={declaration.step_output} />
      </section>
    </div>
  );
}

export function ClassifierInputSchema({ declaration }: { declaration: ClassifierDeclaration }) {
  return (
    <section aria-label="Classifier input" className="min-w-0">
      <h4 className="mt-0 mb-2 font-mono text-[9px] tracking-[.08em] text-muted uppercase">
        Classifier input
      </h4>
      {declaration.input_schema === null ? (
        <p className="m-0 text-[11px] text-muted">
          No input model declared. Accepts JSON state.
        </p>
      ) : (
        <DeclaredSchema label="state" schema={declaration.input_schema} />
      )}
    </section>
  );
}
