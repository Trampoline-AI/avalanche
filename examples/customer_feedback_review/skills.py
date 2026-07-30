import avalanche as ava

evidence_coding_skill = ava.agent.Skill(
    name="evidence-coding",
    instructions="""Turn qualitative records into auditable structured findings.

## When to use

Use this procedure when recurring patterns or material risks must be inferred from many
short records such as survey responses, support tickets, reviews, or interview notes.

## Procedure

1. Inventory the complete dataset before interpreting it. Record row counts, columns,
   identifier uniqueness, nulls, duplicates, and malformed records.
2. Keep deterministic and semantic work separate. Use Python for filtering, joins,
   grouping, deduplication, arithmetic, and coverage checks. Use focused `predict()` calls
   only for judgments such as semantic labels, equivalence, impact, or severity.
3. Process semantic work in bounded batches. Preserve each record's immutable identifier
   and source location in every intermediate result so labels can be joined back without
   relying on row order.
4. Build findings from source-backed record groups. Recalculate every reported count from
   the original records, not from model prose or sampled evidence.
5. Select representative evidence only after the complete group is known. Preserve exact
   excerpts and source locations; never rewrite an excerpt as if it were a quotation.
6. Before submission, verify coverage, identifier uniqueness, aggregate totals, and that
   every claim has evidence. Preserve uncertainty and reject unsupported conclusions.

## Useful Python checks

Use sets to detect repeated identifiers and distinct entities. Use explicit merge
validation where supported, for example `merge(..., validate="many_to_one")`. Compare
group-size totals with the analyzed row count, and assert that numeric rollups equal sums
over deduplicated source entities.

## Failure modes

Do not ask a language model to count rows, perform joins, or total numeric fields. Do not
silently drop null identifiers, coerce malformed records, infer missing relationships, or
promote a single record into a recurring pattern.""",
)
