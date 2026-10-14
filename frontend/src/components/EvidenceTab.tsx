import { useState } from "react";
import type { Chunk } from "../types";
import CodeBlock from "./CodeBlock";

export interface EvidenceTabProps {
  evidence: Chunk[];
}

const GROUP_ORDER = ["endpoint", "class", "interface", "method", "entity", "screen"];

const GROUP_LABELS: Record<string, string> = {
  endpoint: "Endpoints",
  class: "Classes",
  interface: "Interfaces",
  method: "Methods",
  entity: "Entities",
  screen: "Screens",
};

function groupLabel(kind: string): string {
  return GROUP_LABELS[kind] ?? `${kind}s`;
}

function stripCitationPrefix(citation: string): string {
  const idx = citation.indexOf(":");
  return idx === -1 ? citation : citation.slice(idx + 1);
}

function chunkTitle(chunk: Chunk): string | undefined {
  if (!chunk.history) return undefined;
  const h = chunk.history;
  return `${h.commits_all_time} commits (${h.commits_recent} recent) - last changed ${h.last_changed} - module: ${h.module}`;
}

export default function EvidenceTab(props: EvidenceTabProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  if (props.evidence.length === 0) {
    return <p className="text-sm text-slate-400">No evidence yet.</p>;
  }

  const groups = new Map<string, Chunk[]>();
  for (const chunk of props.evidence) {
    const list = groups.get(chunk.kind);
    if (list) {
      list.push(chunk);
    } else {
      groups.set(chunk.kind, [chunk]);
    }
  }

  const knownKinds = GROUP_ORDER.filter((kind) => groups.has(kind));
  const otherKinds = [...groups.keys()]
    .filter((kind) => !GROUP_ORDER.includes(kind))
    .sort((a, b) => a.localeCompare(b));
  const orderedKinds = [...knownKinds, ...otherKinds];

  function toggle(nodeId: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(nodeId)) {
        next.delete(nodeId);
      } else {
        next.add(nodeId);
      }
      return next;
    });
  }

  return (
    <div>
      {orderedKinds.map((kind) => (
        <div key={kind}>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mt-4 first:mt-0 mb-2">
            {groupLabel(kind)}
          </h3>
          <div className="flex flex-col gap-1">
            {groups.get(kind)!.map((chunk) => (
              <div key={chunk.node_id}>
                <button
                  type="button"
                  onClick={() => toggle(chunk.node_id)}
                  title={chunkTitle(chunk)}
                  aria-expanded={expanded.has(chunk.node_id)}
                  className="w-full text-left px-2 py-1.5 rounded-md hover:bg-slate-50"
                >
                  <div className="text-sm font-medium">{chunk.name}</div>
                  <div className="font-mono text-xs text-slate-400">
                    {stripCitationPrefix(chunk.citation)}
                  </div>
                </button>
                {expanded.has(chunk.node_id) && (
                  <div className="mt-1">
                    <CodeBlock code={chunk.code} startLine={chunk.start_line} />
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
