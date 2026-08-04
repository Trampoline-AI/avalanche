export type DescriptorRetention = "older" | "newer";

export interface DescriptorPageState<T> {
  records: T[];
  nextPageToken: string;
  nextCursor: string;
}

export const DESCRIPTOR_PAGE_SIZE = 100;
export const DESCRIPTOR_WINDOW_SIZE = 500;
export const DETAIL_CACHE_MAX_BYTES = 8 * 1024 * 1024;
export const SCROLL_LOAD_THRESHOLD_PX = 96;

export function compareSequence(left: string, right: string) {
  if (left.length !== right.length) return left.length - right.length;
  return left < right ? -1 : left > right ? 1 : 0;
}

export function boundDescriptors<T>(
  recordsBySequence: Map<string, T>,
  sequence: (record: T) => string,
  retention: DescriptorRetention,
  retainedSequences: Iterable<string> = [],
): T[] {
  const merged = [...recordsBySequence.values()].sort((left, right) =>
    compareSequence(sequence(left), sequence(right)),
  );
  if (merged.length <= DESCRIPTOR_WINDOW_SIZE) return merged;

  const retained = new Set(retainedSequences);
  const retainedRecords: T[] = [];
  const availableRecords: T[] = [];
  for (const record of merged) {
    (retained.has(sequence(record)) ? retainedRecords : availableRecords).push(record);
  }
  return [
    ...(retention === "newer"
      ? availableRecords.slice(-(DESCRIPTOR_WINDOW_SIZE - retainedRecords.length))
      : availableRecords.slice(0, DESCRIPTOR_WINDOW_SIZE - retainedRecords.length)),
    ...retainedRecords,
  ]
    .sort((left, right) => compareSequence(sequence(left), sequence(right)))
    .slice(-DESCRIPTOR_WINDOW_SIZE);
}

export function mergeDescriptorPage<T>(
  current: DescriptorPageState<T>,
  next: DescriptorPageState<T>,
  sequence: (record: T) => string,
  retention: DescriptorRetention,
  retainedSequences: Iterable<string> = [],
): DescriptorPageState<T> {
  const recordsBySequence = new Map<string, T>();
  for (const record of current.records) recordsBySequence.set(sequence(record), record);
  for (const record of next.records) recordsBySequence.set(sequence(record), record);
  return {
    ...next,
    records: boundDescriptors(recordsBySequence, sequence, retention, retainedSequences),
  };
}

export function measuredByteCost(value: unknown, reportedSize?: string) {
  const reported = Number(reportedSize);
  let measured = 0;
  try {
    const encoded = typeof value === "string" ? value : JSON.stringify(value) ?? "";
    measured = new TextEncoder().encode(encoded).byteLength;
  } catch {
    measured = DETAIL_CACHE_MAX_BYTES + 1;
  }
  return Math.max(Number.isFinite(reported) && reported > 0 ? reported : 0, measured);
}
