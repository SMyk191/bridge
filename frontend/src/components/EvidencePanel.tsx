import { useState } from "react";
import type { RunDetail } from "../types";
import EvidenceTab from "./EvidenceTab";
import GraphPathTab from "./GraphPathTab";
import TraceTab from "./TraceTab";

export interface EvidencePanelProps {
  run: RunDetail | null;
  loading?: boolean;
}

type TabKey = "evidence" | "graph" | "trace";

const TABS: { key: TabKey; label: string }[] = [
  { key: "evidence", label: "Evidence" },
  { key: "graph", label: "Graph path" },
  { key: "trace", label: "Trace" },
];

export default function EvidencePanel(props: EvidencePanelProps) {
  const [activeTab, setActiveTab] = useState<TabKey>("evidence");

  return (
    <div className="h-full flex flex-col">
      <div className="flex border-b border-slate-200">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => setActiveTab(tab.key)}
            className={
              tab.key === activeTab
                ? "px-3 py-2 text-sm border-b-2 border-indigo-600 text-indigo-700 font-medium"
                : "px-3 py-2 text-sm text-slate-500 hover:text-slate-700"
            }
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div className="overflow-y-auto flex-1 p-3">
        {props.loading ? (
          <p className="text-sm text-slate-400">Loading...</p>
        ) : props.run === null ? (
          <p className="text-sm text-slate-400">
            Ask a question to see evidence, graph path, and trace here.
          </p>
        ) : activeTab === "evidence" ? (
          <EvidenceTab evidence={props.run.evidence} />
        ) : activeTab === "graph" ? (
          <GraphPathTab
            evidence={props.run.evidence}
            hubsCrossed={props.run.impact_map?.hubs_crossed ?? []}
          />
        ) : (
          <TraceTab trace={props.run.trace} usage={props.run.usage} />
        )}
      </div>
    </div>
  );
}
