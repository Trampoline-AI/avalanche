function readableDuration(milliseconds: number): string {
  if (milliseconds <= 120_000) return `${milliseconds / 1000}s`;
  const seconds = Math.floor(milliseconds / 1000);
  const minutes = Math.floor(seconds / 60);
  if (milliseconds <= 69 * 60_000) return `${minutes}min${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h${minutes % 60}min${seconds % 60}s`;
}

export function formatLogDuration(message: string): string {
  // Only format the operator's agent-event duration field, not user log payloads.
  return message.replace(
    /^(Agent [\w.]+(?: iteration=\d+)? duration=)(\d+)ms(?=\s|$)(?! \()/,
    (match: string, prefix: string, rawMilliseconds: string) => {
      const milliseconds = Number(rawMilliseconds);
      if (!Number.isSafeInteger(milliseconds) || milliseconds < 1000) return match;
      return `${prefix}${readableDuration(milliseconds)} (${rawMilliseconds}ms)`;
    },
  );
}
