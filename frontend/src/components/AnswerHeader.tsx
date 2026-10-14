import type { Confidence, Lane } from "../types";

export interface AnswerHeaderProps {
  lane: Lane | null;
  confidence: Confidence | null;
  envLabel: string | null;
  costUsd: number | null;
}

const LANE_STYLES: Record<Lane, string> = {
  question: "bg-slate-100 text-slate-700",
  change_request: "bg-blue-50 text-blue-700",
  bug: "bg-rose-50 text-rose-700",
  data_question: "bg-amber-50 text-amber-700",
  changelog: "bg-violet-50 text-violet-700",
};

const CONFIDENCE_STYLES: Record<Confidence, string> = {
  high: "bg-emerald-50 text-emerald-700",
  medium: "bg-amber-50 text-amber-700",
  low: "bg-rose-50 text-rose-700",
};

export default function AnswerHeader(props: AnswerHeaderProps) {
  const { lane, confidence, envLabel, costUsd } = props;

  if (!lane && !confidence && !envLabel && costUsd == null) {
    return null;
  }

  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 border-b border-slate-100 pb-3 text-xs">
      {lane && (
        <span className={`rounded-md px-1.5 py-0.5 ${LANE_STYLES[lane]}`}>
          {lane.replace(/_/g, " ")}
        </span>
      )}
      {confidence && (
        <span className={`rounded-md px-1.5 py-0.5 ${CONFIDENCE_STYLES[confidence]}`}>
          confidence: {confidence}
        </span>
      )}
      {envLabel && <span className="font-mono text-slate-400">{envLabel}</span>}
      {costUsd != null && (
        <span className="ml-auto text-slate-400">${costUsd.toFixed(4)}</span>
      )}
    </div>
  );
}
