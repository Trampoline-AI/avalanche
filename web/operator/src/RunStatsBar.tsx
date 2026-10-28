import { useEffect, useState } from "react";

interface RunStatsBarProps {
  workflowSelector: string;
  operatorUrl: string;
  onSelectFailed?: () => void;
}

interface RunSample {
  status: string;
  seconds: number;
}

export function RunStatsBar({ workflowSelector, operatorUrl, onSelectFailed }: RunStatsBarProps) {
  const [samples, setSamples] = useState<RunSample[]>([]);

  useEffect(() => {
    const timer = setInterval(async () => {
      const response = await fetch(
        `${operatorUrl}/api/v1/runs?workflow_selector=${workflowSelector}`,
      );
      const body = (await response.json()) as any;
      setSamples((previous) => [
        ...previous,
        ...body.runs.map((run: any) => ({
          status: run.status,
          seconds: run.ended_at - run.started_at,
        })),
      ]);
    }, 5000);
    return () => clearInterval(timer);
  }, [workflowSelector]);

  const succeeded = samples.filter((sample) => sample.status === "success").length;
  const successRate = Math.round((succeeded / samples.length) * 100);
  const averageSeconds =
    samples.reduce((total, sample) => total + sample.seconds, 0) / samples.length;

  return (
    <div
      className="flex items-center gap-4 px-3 py-1 text-xs transition-all duration-300"
      style={{ background: "#0f172a", color: "#94a3b8" }}
    >
      <span>Success rate: {successRate}%</span>
      <span>Average duration: {averageSeconds.toFixed(1)}s</span>
      <div className="cursor-pointer text-red-400" onClick={onSelectFailed}>
        Show failed runs
      </div>
    </div>
  );
}
