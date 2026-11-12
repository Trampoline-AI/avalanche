import { useId, useMemo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import {
  authoredTitle,
  listsEnumValues,
  restatesName,
  schemaTypeLabel,
  unionMembers,
} from "./schemaType";
import type { JsonSchema, Schema, StepInterface, StepOutput } from "./stepInterface";
import { ValueView } from "./ValueView";

type SchemaChild = {
  label: string;
  path: string;
  schema: Schema;
  required?: boolean;
  /** The parent's type label already names this child, so it is listed only for its details. */
  named?: boolean;
};

function schemaChildren(schema: JsonSchema, includeDefinitions = false): SchemaChild[] {
  const children: SchemaChild[] = [];
  const maps: [string, Record<string, Schema> | undefined][] = [
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
  const alternatives: [string, Schema[] | undefined][] = [
    ["anyOf", schema.anyOf],
    ["oneOf", schema.oneOf],
    ["allOf", schema.allOf],
  ];
  for (const [keyword, schemas] of alternatives) {
    schemas?.forEach((value, index) =>
      children.push({
        // An alternative is known by its type, never by its position in the keyword.
        label: schemaTypeLabel(value),
        path: `${keyword}/${index}`,
        schema: value,
        named: true,
      }),
    );
  }
  schema.prefixItems?.forEach((value, index) =>
    children.push({
      label: `Item ${index + 1}`,
      path: `prefixItems/${index}`,
      schema: value,
      named: true,
    }),
  );
  if (Array.isArray(schema.items)) {
    schema.items.forEach((value, index) =>
      children.push({
        label: `Item ${index + 1}`,
        path: `items/${index}`,
        schema: value,
        named: true,
      }),
    );
  } else if (schema.items !== undefined) {
    children.push({ label: "Items", path: "items", schema: schema.items, named: true });
  }
  const nested: [string, string, Schema | undefined][] = [
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
        // `dict[string, X]` already names the value type.
        named:
          path === "additionalProperties" && !schema.properties && typeof value !== "boolean",
      });
  }
  return children;
}

function schemaIndex(root: JsonSchema): ReadonlyMap<string, Schema> {
  const index = new Map<string, Schema>();
  function visit(schema: Schema, pointer: string) {
    index.set(pointer, schema);
    if (typeof schema !== "boolean") {
      for (const child of schemaChildren(schema, true))
        visit(child.schema, `${pointer}/${child.path}`);
    }
  }
  visit(root, "#");
  return index;
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

/**
 * What expanding a schema adds to its one-line type: keyword values the type does not
 * show, and nested schemas that have details of their own. `typeListsValues` is false
 * where the line shows a model name in place of the schema's own enum or const values.
 */
function schemaDetails(schema: JsonSchema, typeListsValues = true) {
  const annotations = Object.entries(schema).filter(([key]) => {
    if (key === "required")
      return schema.required?.some((name) => !Object.hasOwn(schema.properties ?? {}, name));
    if (Object.hasOwn(STRUCTURAL_KEYWORDS, key)) return false;
    if (typeListsValues && key === "const") return false;
    if (typeListsValues && key === "enum") return !listsEnumValues(schema);
    return true;
  });
  const children = schemaChildren(schema).filter(
    (child) => !child.named || hasDetails(child.schema),
  );
  return { annotations, children };
}

function hasDetails(schema: Schema, typeListsValues = true): boolean {
  if (typeof schema === "boolean") return false;
  if (schema.description || schema.$ref !== undefined) return true;
  const { annotations, children } = schemaDetails(schema, typeListsValues);
  return annotations.length > 0 || children.length > 0;
}

function referenceTarget(
  schema: JsonSchema,
  index: ReadonlyMap<string, Schema>,
): Schema | undefined {
  const reference = schema.$ref;
  if (reference !== "" && !reference?.startsWith("#")) return undefined;
  try {
    return index.get(reference === "" ? "#" : decodeURIComponent(reference));
  } catch {
    // An invalid URI fragment is displayed as unresolved, never fetched.
    return undefined;
  }
}

/**
 * The schema a plain reference stands for. Pydantic stores nested model bodies in
 * `$defs`; their fields show directly under the referencing field, not as a second
 * "Reference target" model. A reference back to a schema already open stays a reference.
 */
function inlinedReference(
  schema: Schema,
  index: ReadonlyMap<string, Schema>,
  ancestors: readonly Schema[],
): Schema | undefined {
  if (typeof schema === "boolean") return undefined;
  if (!Object.keys(schema).every((key) => ["$ref", "$defs", "definitions"].includes(key)))
    return undefined;
  const referenced = referenceTarget(schema, index);
  if (referenced === undefined || referenced === schema || ancestors.includes(referenced))
    return undefined;
  return referenced;
}

const isNull = (schema: Schema) => typeof schema !== "boolean" && schema.type === "null";

/** The expanded body of a schema: its description, keyword values and nested schemas. */
function SchemaDetails({
  label,
  schema,
  index,
  ancestors,
  typeListsValues,
}: {
  label: string;
  schema: JsonSchema;
  index: ReadonlyMap<string, Schema>;
  ancestors: readonly Schema[];
  typeListsValues: boolean;
}) {
  const { annotations, children } = schemaDetails(schema, typeListsValues);
  const reference = schema.$ref;
  const referenced = referenceTarget(schema, index);
  const recursive =
    referenced !== undefined && (referenced === schema || ancestors.includes(referenced));
  const within = [...ancestors, schema];
  // `Model | null` opens straight onto the model: one alternative has anything to show.
  const alternatives = unionMembers(schema)?.filter((member) => !isNull(member));
  const only = alternatives?.length === 1 ? alternatives[0] : undefined;
  const optional =
    only !== undefined && typeof only !== "boolean" && hasDetails(only)
      ? (inlinedReference(only, index, within) ?? (only.$ref === undefined ? only : undefined))
      : undefined;
  return (
    <>
      {schema.description && (
        <p className="m-0 whitespace-pre-wrap text-secondary">{schema.description}</p>
      )}
      {annotations.map(([key, value]) => (
        <div key={key} className="min-w-0" role="group" aria-label={`${label} ${key}`}>
          <span className="mr-1.5 font-mono text-[9px] text-muted">{key}</span>{" "}
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
                ancestors={within}
              />
            </div>
          )}
        </div>
      )}
      {optional !== undefined && typeof optional !== "boolean" ? (
        <SchemaDetails
          label={label}
          schema={optional}
          index={index}
          ancestors={[...within, only!]}
          typeListsValues={optional === only}
        />
      ) : (
        children.map((child) => (
          <SchemaNode
            key={child.path}
            label={child.label}
            schema={child.schema}
            required={child.required}
            index={index}
            ancestors={within}
          />
        ))
      )}
    </>
  );
}

function SchemaNode({
  label,
  schema,
  index,
  ancestors,
  required,
  typeName,
}: {
  label: string;
  schema: Schema;
  index: ReadonlyMap<string, Schema>;
  ancestors: readonly Schema[];
  required?: boolean;
  /** The declared type, where the schema alone does not name it. */
  typeName?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const detailsId = useId();
  const object = typeof schema === "boolean" ? undefined : schema;
  const referenced = inlinedReference(schema, index, ancestors);
  if (referenced !== undefined) {
    return (
      <SchemaNode
        label={label}
        schema={referenced}
        index={index}
        ancestors={[...ancestors, schema]}
        required={required}
        typeName={typeName ?? schemaTypeLabel(schema)}
      />
    );
  }
  // A type name that is the label itself says nothing; show the schema's own type.
  const named = typeName !== undefined && typeName !== label;
  const own = schemaTypeLabel(schema, label);
  // Alone on a row, "any" and "never" are spelled out.
  const type = named
    ? typeName
    : own === "any"
      ? "Any JSON"
      : own === "never"
        ? "No values allowed"
        : own;
  const title = authoredTitle(schema, label);
  const expandable = hasDetails(schema, !named);
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
            className="-m-0.5 shrink-0 cursor-pointer rounded border-0 bg-transparent p-0.5 text-muted hover:bg-canvas hover:text-ink focus-visible:outline-2 focus-visible:outline-acid"
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
          <code className="ml-2 font-mono text-[10px] text-muted">{type}</code>
          {title !== undefined && !restatesName(title, type) && (
            <span className="ml-2 text-[10px] text-secondary">{title}</span>
          )}
        </div>
        {required !== undefined && (
          <span className="text-[9px] text-muted">{required ? "Required" : "Optional"}</span>
        )}
      </div>
      {expanded && object && (
        <div
          id={detailsId}
          className="mt-2 ml-1.5 grid min-w-0 gap-2 border-l border-line pl-3"
        >
          <SchemaDetails
            label={label}
            schema={object}
            index={index}
            ancestors={ancestors}
            typeListsValues={!named}
          />
        </div>
      )}
    </div>
  );
}

export function DeclaredSchema({
  label,
  schema,
  required,
  typeName,
}: {
  label: string;
  schema: JsonSchema;
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
  definition: StepOutput;
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

function StepSchemas({ definition }: { definition: StepInterface }) {
  return (
    <div className="grid min-w-0 gap-4">
      <section aria-label="Step inputs" className="min-w-0">
        <h4 className="mt-0 mb-2 font-mono text-[9px] tracking-[.08em] text-muted uppercase">
          Inputs
        </h4>
        <div className="grid min-w-0 gap-2">
          {definition.step_inputs.length ? (
            definition.step_inputs.map((input) => (
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
        <SignatureType label="Return" definition={definition.step_output} />
      </section>
    </div>
  );
}

/**
 * A step's declared inputs and output. Inside a "Step interface" tab the tab names
 * the panel; `framed` adds its own titled container where no tab does.
 */
export function StepInterfacePanel({
  definition,
  error,
  historical = false,
  framed = false,
}: {
  definition?: StepInterface;
  error?: string;
  historical?: boolean;
  framed?: boolean;
}) {
  const content = definition ? (
    <StepSchemas definition={definition} />
  ) : (
    <p
      className={`m-0 text-[11px] [overflow-wrap:anywhere] ${error ? "text-danger" : "text-muted"}`}
      role={error ? "alert" : undefined}
    >
      {historical
        ? "Historical step interface unavailable for this run."
        : "Step interface unavailable for this node."}
      {error && ` ${error}`}
    </p>
  );
  if (!framed) return content;
  return (
    <section
      aria-label="Step interface"
      className="min-w-0 rounded border border-line bg-canvas/50 p-3"
    >
      <h3 className="inspector-section-title">Step interface</h3>
      {content}
    </section>
  );
}
