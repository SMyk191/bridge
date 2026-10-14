import type { TraceEntry, Usage } from "../types";

export interface TraceTabProps {
  trace: TraceEntry[];
  usage: Usage | null;
}

const STATUS_STYLES: Record<TraceEntry["status"], string> = {
  ok: "bg-emerald-600 text-emerald-600",
  error: "bg-rose-600 text-rose-600",
  budget_exceeded: "bg-rose-600 text-rose-600",
  skipped: "bg-slate-400 text-slate-400",
  running: "bg-indigo-600 text-indigo-600",
};

export default function TraceTab(props: TraceTabProps) {
  const { trace, usage } = props;

  if (trace.length === 0) {
    return <p className="text-sm text-slate-400">No trace yet.</p>;
  }

  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-slate-500">
              <th className="py-1 pr-3 font-medium">Node</th>
              <th className="py-1 pr-3 font-medium">Status</th>
              <th className="py-1 pr-3 font-medium">Duration</th>
              <th className="py-1 pr-3 font-medium">Tokens</th>
              <th className="py-1 pr-3 font-medium">Cost</th>
            </tr>
          </thead>
          <tbody>
            {trace.map((entry, index) => {
              const [dotClass, textClass] = STATUS_STYLES[entry.status].split(" ");
              return (
                <tr key={`${entry.node}-${index}`} className="border-t border-slate-100">
                  <td className="py-1.5 pr-3 align-top">
                    <span className="font-mono text-slate-900">{entry.node}</span>
                    {entry.detail !== "" && (
                      <p className="text-slate-400 text-[11px]">{entry.detail}</p>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 align-top">
                    <span className="inline-flex items-center gap-1.5">
                      <span className={`h-1.5 w-1.5 rounded-full ${dotClass}`} />
                      <span className={textClass}>{entry.status}</span>
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 align-top text-slate-700">
                    {entry.elapsed_s.toFixed(2)}s
                  </td>
                  <td className="py-1.5 pr-3 align-top text-slate-700">
                    {entry.input_tokens.toLocaleString()}
                  </td>
                  <td className="py-1.5 pr-3 align-top text-slate-700">
                    ${entry.cost_usd.toFixed(4)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {usage !== null && (
        <div className="border-t border-slate-100 pt-2 mt-2 text-xs text-slate-500">
          {usage.calls.toLocaleString()} calls · {usage.input_tokens.toLocaleString()} input
          tokens · ${usage.cost_usd.toFixed(4)}
        </div>
      )}
    </div>
  );
}
