import type { JsonSchema, Schema } from "./stepInterface";

/** Longer enums read as a wall of text; their values stay listed under the field. */
const MAX_INLINE_ENUM_VALUES = 8;

const comparable = (text: string) => text.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");

/** True when two names differ only by case, spacing or punctuation. */
export function restatesName(text: string, name: string): boolean {
  return comparable(text) === comparable(name);
}

/**
 * The title an author chose for a schema. A title that only restates the field name,
 * such as the "Folder Id" Pydantic generates for `folder_id`, is not one.
 */
export function authoredTitle(schema: Schema, fieldName?: string): string | undefined {
  if (typeof schema === "boolean" || !schema.title) return undefined;
  if (fieldName !== undefined && restatesName(schema.title, fieldName)) return undefined;
  return schema.title;
}

/** The definition name a local `$ref` points at. */
export function referenceName(reference: string): string {
  const name = reference.split("/").at(-1)?.replace(/~1/g, "/").replace(/~0/g, "~");
  return name || reference || "Reference";
}

/** `anyOf` and `oneOf` alternatives, unless they only constrain a schema that has a type. */
export function unionMembers(schema: JsonSchema): Schema[] | undefined {
  if (schema.$ref !== undefined || schema.type !== undefined) return undefined;
  return schema.anyOf ?? schema.oneOf;
}

/** True when the type label already lists every enum value. */
export function listsEnumValues(schema: JsonSchema): boolean {
  return schema.enum !== undefined && schema.enum.length <= MAX_INLINE_ENUM_VALUES;
}

function unique(labels: string[]): string[] {
  return [...new Set(labels)];
}

function union(members: Schema[]): string {
  return unique(
    members.map((member) => {
      const label = schemaTypeLabel(member);
      return typeof member !== "boolean" && (member.allOf?.length ?? 0) > 1 && !member.type
        ? `(${label})`
        : label;
    }),
  ).join(" | ");
}

function arrayLabel(schema: JsonSchema): string {
  const tuple = schema.prefixItems ?? (Array.isArray(schema.items) ? schema.items : undefined);
  if (tuple) return `tuple[${tuple.map((item) => schemaTypeLabel(item)).join(", ")}]`;
  if (schema.items === undefined || Array.isArray(schema.items)) return "array";
  return `${schema.uniqueItems ? "set" : "list"}[${schemaTypeLabel(schema.items)}]`;
}

function objectLabel(schema: JsonSchema, fieldName?: string): string {
  const title = authoredTitle(schema, fieldName);
  if (title) return title;
  const values = schema.additionalProperties;
  if (!schema.properties && values !== undefined && typeof values !== "boolean")
    return `dict[string, ${schemaTypeLabel(values)}]`;
  return "object";
}

/**
 * A schema's type in Python-like notation: `string | null`, `list[Address]`,
 * `"draft" | "sent"`. Alternatives are joined with ` | `, references and titled
 * objects read as their model name. `fieldName` is the name the schema is shown
 * under, so a title that only restates it is not taken for a model name.
 */
export function schemaTypeLabel(schema: Schema, fieldName?: string): string {
  if (typeof schema === "boolean") return schema ? "any" : "never";
  if (schema.$ref !== undefined) return referenceName(schema.$ref);
  if (schema.const !== undefined) return JSON.stringify(schema.const);
  if (schema.enum)
    return listsEnumValues(schema)
      ? unique(schema.enum.map((value) => JSON.stringify(value))).join(" | ")
      : "enum";
  const members = unionMembers(schema);
  if (members) return union(members);
  if (Array.isArray(schema.type))
    return schema.type
      .map((type) =>
        type === "array"
          ? arrayLabel(schema)
          : type === "object"
            ? objectLabel(schema, fieldName)
            : type,
      )
      .join(" | ");
  if (schema.type === "array") return arrayLabel(schema);
  if (schema.type === "object") return objectLabel(schema, fieldName);
  if (schema.type) return schema.type;
  if (schema.allOf)
    return unique(schema.allOf.map((member) => schemaTypeLabel(member))).join(" & ");
  if (schema.properties) return objectLabel(schema, fieldName);
  if (schema.items !== undefined || schema.prefixItems) return arrayLabel(schema);
  return "any";
}
