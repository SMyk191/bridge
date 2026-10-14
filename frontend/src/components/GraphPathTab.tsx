import type { Chunk, ImpactEntry } from "../types";

export interface GraphPathTabProps {
  evidence: Chunk[];
  hubsCrossed: ImpactEntry[];
}

export default function GraphPathTab(props: GraphPathTabProps) {
  const { evidence, hubsCrossed } = props;

  if (evidence.length === 0) {
    return <p className="text-sm text-slate-400">No graph path yet.</p>;
  }

  const sorted = [...evidence].sort((a, b) => {
    const depthA = a.graph_depth ?? 0;
    const depthB = b.graph_depth ?? 0;
    if (depthA !== depthB) return depthA - depthB;
    return a.name.localeCompare(b.name);
  });

  // node_id, not name -- name is LLM-phrased free text from the cartographer
  // merge and isn't guaranteed to string-match a chunk's name verbatim, while
  // node_id is the stable machine-generated key both sides derive from the
  // same known[citation] lookup table.
  const hubNodeIds = new Set(hubsCrossed.map((hub) => hub.node_id));

  return (
    <div className="flex flex-col gap-2">
      {sorted.map((chunk) => {
        const depth = chunk.graph_depth ?? 0;
        const isHub = hubNodeIds.has(chunk.node_id);
        return (
          <div key={chunk.node_id} style={{ paddingLeft: depth * 16 }}>
            <div className="flex items-center gap-1.5">
              <span className="text-sm font-mono text-slate-900">{chunk.name}</span>
              {isHub && (
                <span className="rounded px-1 py-0.5 text-[10px] bg-amber-50 text-amber-700">
                  hub
                </span>
              )}
            </div>
            {chunk.graph_path !== null && (
              <p className="text-xs text-slate-400 font-mono">{chunk.graph_path}</p>
            )}
          </div>
        );
      })}
    </div>
  );
}
