export function formatElapsedSeconds(seconds: number): string {
  if (seconds <= 120) return `${seconds.toFixed(1)}s`;
  const wholeSeconds = Math.floor(seconds);
  const minutes = Math.floor(wholeSeconds / 60);
  const remainingSeconds = wholeSeconds % 60;
  if (seconds <= 69 * 60) return `${minutes}m ${remainingSeconds}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m ${remainingSeconds}s`;
}
