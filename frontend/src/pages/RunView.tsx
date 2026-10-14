import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getRun, postFeedback, postVocabProposal, runPermalink } from "../api";
import AnswerFooter from "../components/AnswerFooter";
import AnswerHeader from "../components/AnswerHeader";
import DraftTicketModal from "../components/DraftTicketModal";
import EvidencePanel from "../components/EvidencePanel";
import MessageBubble from "../components/MessageBubble";
import type { ImpactMap, RunDetail, Vote } from "../types";

function impactMapEntities(map: ImpactMap | null): string[] {
  if (!map) return [];
  const ids = [
    ...map.endpoints,
    ...map.classes_methods,
    ...map.entities_tables,
    ...map.screens,
    ...map.hubs_crossed,
  ]
    .map((entry) => entry.node_id)
    .filter(Boolean);
  return Array.from(new Set(ids));
}

/** `GET /runs/:id` rendered read-only — a shareable permalink for one run. */
export default function RunView() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [vote, setVote] = useState<Vote | null>(null);
  const [ticketOpen, setTicketOpen] = useState(false);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    setError(null);
    getRun(id)
      .then(setRun)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, [id]);

  const handleVote = useCallback(
    (v: Vote) => {
      if (!id) return;
      setVote(v);
      postFeedback(id, v).catch((err) => console.error("postFeedback failed", err));
    },
    [id],
  );

  const handleCorrection = useCallback((term: string, entities: string[], note?: string) => {
    postVocabProposal(term, entities, note).catch((err) =>
      console.error("postVocabProposal failed", err),
    );
  }, []);

  const candidateEntities = useMemo(() => impactMapEntities(run?.impact_map ?? null), [run]);

  return (
    <div className="grid h-full min-h-0 grid-cols-[1fr_360px]">
      <main className="overflow-y-auto min-h-0 p-4 space-y-4 min-w-0">
        <Link to="/" className="text-xs text-indigo-600 hover:underline">
          &larr; Back to Bridge
        </Link>

        {loading && <p className="text-sm text-slate-400">Loading run...</p>}
        {error && <p className="text-sm text-rose-600">{error}</p>}

        {run && (
          <div className="space-y-2">
            <MessageBubble role="user" content={run.request} />
            <MessageBubble
              role="assistant"
              content={run.answer}
              selected
              header={
                <AnswerHeader
                  lane={run.lane}
                  confidence={run.impact_map?.confidence ?? null}
                  envLabel={run.env_label}
                  costUsd={run.usage?.cost_usd ?? null}
                />
              }
              footer={
                <AnswerFooter
                  runId={run.run_id}
                  hasSpec={Boolean(run.spec)}
                  votedValue={vote}
                  onVote={handleVote}
                  onSubmitCorrection={handleCorrection}
                  candidateEntities={candidateEntities}
                  onCopyPermalink={() => navigator.clipboard.writeText(runPermalink(run.run_id))}
                  onDraftTicket={() => setTicketOpen(true)}
                />
              }
            />
          </div>
        )}
      </main>

      <aside className="border-l border-slate-200 min-w-0 h-full min-h-0">
        <EvidencePanel run={run} loading={loading} />
      </aside>

      {run && (
        <DraftTicketModal
          open={ticketOpen}
          onClose={() => setTicketOpen(false)}
          request={run.request}
          lane={run.lane}
          spec={run.spec}
        />
      )}
    </div>
  );
}
