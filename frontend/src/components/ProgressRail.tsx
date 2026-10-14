export interface ProgressStage {
  key: string;
  label: string;
  status: "pending" | "active" | "done" | "skipped" | "error";
  detail?: string;
}

export interface ProgressRailProps {
  stages: ProgressStage[];
}

const STATUS_CLASSES: Record<ProgressStage["status"], string> = {
  done: "text-slate-500",
  active: "text-indigo-700 font-medium",
  pending: "text-slate-400",
  skipped: "text-slate-300 line-through",
  error: "text-rose-600 font-medium",
};

export default function ProgressRail(props: ProgressRailProps) {
  const { stages } = props;
  if (stages.length === 0) return null;

  // No stage's detail is ever populated while it's "active" — the backend's
  // node_started event always carries an empty detail, and node_finished sets
  // both the real detail string and "done" in the same transition. So the most
  // recently completed stage's detail is the only real live text available,
  // and stays a useful caption while the next stage runs.
  const activeStage = [...stages].reverse().find((stage) => stage.detail);

  return (
    <div>
      <div className="flex flex-wrap items-center gap-1.5">
        {stages.map((stage, index) => (
          <div key={stage.key} className="flex items-center gap-1.5">
            {index > 0 && <span className="text-slate-300">&rsaquo;</span>}
            <span className={`inline-flex items-center gap-1 text-xs ${STATUS_CLASSES[stage.status]}`}>
              {stage.status === "done" && <span aria-hidden="true">&#10003;</span>}
              {stage.status === "active" && (
                <span className="h-1.5 w-1.5 rounded-full bg-indigo-600 animate-pulse" aria-hidden="true" />
              )}
              {stage.label}
            </span>
          </div>
        ))}
      </div>
      {activeStage && <p className="mt-1 text-xs text-slate-500">{activeStage.detail}</p>}
    </div>
  );
}
