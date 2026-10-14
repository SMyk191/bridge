import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getRun,
  getSuggestions,
  getThread,
  listThreads,
  postFeedback,
  postVocabProposal,
  runPermalink,
} from "../api";
import AnswerFooter from "../components/AnswerFooter";
import AnswerHeader from "../components/AnswerHeader";
import DraftTicketModal from "../components/DraftTicketModal";
import EvidencePanel from "../components/EvidencePanel";
import MessageBubble from "../components/MessageBubble";
import ProgressRail from "../components/ProgressRail";
import ThreadsSidebar from "../components/ThreadsSidebar";
import { useRunStream } from "../hooks/useRunStream";
import type { ImpactMap, RunDetail, Suggestion, ThreadSummary, Vote } from "../types";

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

export default function Home() {
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [threadsLoading, setThreadsLoading] = useState(true);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [suggestionsLoading, setSuggestionsLoading] = useState(true);

  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const [activeThreadRuns, setActiveThreadRuns] = useState<RunDetail[]>([]);
  const [threadLoading, setThreadLoading] = useState(false);

  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [ticketModalRunId, setTicketModalRunId] = useState<string | null>(null);
  const [votes, setVotes] = useState<Record<string, Vote | null>>({});
  const [question, setQuestion] = useState("");
  // The composer clears immediately on submit for a snappy input — this holds
  // the text that's actually in flight, for the live transcript bubble to show.
  const [pendingQuestion, setPendingQuestion] = useState("");
  // Bumped by every action that changes "what thread/run is current" (new
  // thread, switching threads, asking a fresh question). The async getRun()
  // in the onDone callback below checks this before applying its result, so a
  // stale completion from a run the user has since navigated away from can't
  // clobber activeThreadId/selectedRunId or abort a newer in-flight stream.
  const runContextEpochRef = useRef(0);

  const refreshThreads = useCallback(() => {
    listThreads()
      .then(setThreads)
      .catch((err) => console.error("listThreads failed", err))
      .finally(() => setThreadsLoading(false));
  }, []);

  useEffect(() => {
    refreshThreads();
    getSuggestions()
      .then((res) => setSuggestions(res.questions))
      .catch((err) => console.error("getSuggestions failed", err))
      .finally(() => setSuggestionsLoading(false));
  }, [refreshThreads]);

  const stream = useRunStream((finalState) => {
    if (!finalState.runId) return;
    const epoch = runContextEpochRef.current;
    getRun(finalState.runId)
      .then((full) => {
        if (runContextEpochRef.current !== epoch) return; // stale: user moved on
        setActiveThreadRuns((prev) =>
          prev.some((r) => r.run_id === full.run_id) ? prev : [...prev, full],
        );
        setActiveThreadId(full.thread_id);
        setSelectedRunId(full.run_id);
        refreshThreads();
        stream.reset();
      })
      .catch((err) => {
        console.error("getRun after done failed", err);
        // Without this, isStreamingIntoThisThread stays true forever (the
        // live bubble never clears) and the next question would silently
        // discard this already-finished answer instead of showing it.
        if (runContextEpochRef.current === epoch) stream.reset();
      });
  });

  const selectThread = useCallback((threadId: string) => {
    runContextEpochRef.current += 1;
    stream.reset();
    setActiveThreadId(threadId);
    setThreadLoading(true);
    setActiveThreadRuns([]);
    setTicketModalRunId(null);
    getThread(threadId)
      .then(async (detail) => {
        const results = await Promise.allSettled(detail.runs.map((r) => getRun(r.run_id)));
        const full = results
          .filter((r): r is PromiseFulfilledResult<RunDetail> => r.status === "fulfilled")
          .map((r) => r.value);
        const failed = results.length - full.length;
        if (failed > 0) {
          console.error(`getRun failed for ${failed}/${results.length} runs in thread ${threadId}`);
        }
        setActiveThreadRuns(full);
        setSelectedRunId(full.length ? full[full.length - 1].run_id : null);
      })
      .catch((err) => console.error("getThread failed", err))
      .finally(() => setThreadLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const newThread = useCallback(() => {
    runContextEpochRef.current += 1;
    stream.reset();
    setActiveThreadId(null);
    setActiveThreadRuns([]);
    setSelectedRunId(null);
    setTicketModalRunId(null);
    setQuestion("");
    setPendingQuestion("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const ask = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || stream.state.status === "streaming") return;
      runContextEpochRef.current += 1;
      setQuestion("");
      setPendingQuestion(trimmed);
      stream.start(trimmed, activeThreadId);
    },
    [activeThreadId, stream],
  );

  const handleVote = useCallback((runId: string, vote: Vote) => {
    setVotes((prev) => ({ ...prev, [runId]: vote }));
    postFeedback(runId, vote).catch((err) => console.error("postFeedback failed", err));
  }, []);

  const handleCorrection = useCallback(
    (term: string, entities: string[], note?: string) => {
      postVocabProposal(term, entities, note).catch((err) =>
        console.error("postVocabProposal failed", err),
      );
    },
    [],
  );

  const selectedRun = useMemo(
    () => activeThreadRuns.find((r) => r.run_id === selectedRunId) ?? null,
    [activeThreadRuns, selectedRunId],
  );
  const ticketRun = useMemo(
    () => activeThreadRuns.find((r) => r.run_id === ticketModalRunId) ?? null,
    [activeThreadRuns, ticketModalRunId],
  );

  const isStreamingIntoThisThread =
    stream.state.status !== "idle" &&
    !activeThreadRuns.some((r) => r.run_id === stream.state.runId) &&
    (activeThreadId === null || stream.state.threadId === activeThreadId || stream.state.threadId === null);

  // threadLoading excluded so switching to a non-empty thread doesn't flash
  // chips during the fetch, before activeThreadRuns has anything in it yet.
  const showSuggestions =
    activeThreadRuns.length === 0 && !isStreamingIntoThisThread && !threadLoading;

  return (
    <div className="grid h-full min-h-0 grid-cols-[280px_1fr_360px]">
      <aside className="border-r border-slate-200 h-full min-h-0">
        <ThreadsSidebar
          threads={threads}
          activeThreadId={activeThreadId}
          onSelectThread={selectThread}
          onNewThread={newThread}
          loading={threadsLoading}
          suggestions={suggestions}
          suggestionsLoading={suggestionsLoading}
          showSuggestions={showSuggestions}
          onPickSuggestion={ask}
        />
      </aside>

      <main className="flex flex-col min-w-0 min-h-0">
        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
          {threadLoading && <p className="text-sm text-slate-400">Loading thread...</p>}

          {activeThreadRuns.map((run) => (
            <div key={run.run_id} className="space-y-2">
              <MessageBubble role="user" content={run.request} />
              <MessageBubble
                role="assistant"
                content={run.answer}
                selected={selectedRunId === run.run_id}
                onClick={() => setSelectedRunId(run.run_id)}
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
                    votedValue={votes[run.run_id] ?? null}
                    onVote={(vote) => handleVote(run.run_id, vote)}
                    onSubmitCorrection={handleCorrection}
                    candidateEntities={impactMapEntities(run.impact_map)}
                    onCopyPermalink={() => navigator.clipboard.writeText(runPermalink(run.run_id))}
                    onDraftTicket={() => setTicketModalRunId(run.run_id)}
                  />
                }
              />
            </div>
          ))}

          {isStreamingIntoThisThread && (
            <div className="space-y-2">
              <MessageBubble role="user" content={pendingQuestion} />
              <MessageBubble
                role="assistant"
                streaming={stream.state.status === "streaming"}
                content={stream.state.answer || stream.state.impactMap?.summary || ""}
                selected
                header={
                  <>
                    <ProgressRail stages={stream.state.stages} />
                    {(stream.state.lane || stream.state.confidence) && (
                      <div className="mt-2">
                        <AnswerHeader
                          lane={stream.state.lane}
                          confidence={stream.state.confidence}
                          envLabel={null}
                          costUsd={stream.state.costUsd}
                        />
                      </div>
                    )}
                  </>
                }
              />
              {stream.state.errorDetail && (
                <p className="text-sm text-rose-600">{stream.state.errorDetail}</p>
              )}
            </div>
          )}
        </div>

        <form
          className="border-t border-slate-200 p-3 flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            ask(question);
          }}
        >
          <input
            className="flex-1 rounded-md border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-400"
            placeholder="Ask a product-language question about the codebase..."
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            disabled={stream.state.status === "streaming"}
          />
          <button
            type="submit"
            className="rounded-md bg-indigo-600 text-white px-4 py-2 text-sm hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={stream.state.status === "streaming" || !question.trim()}
          >
            Ask
          </button>
        </form>
      </main>

      <aside className="border-l border-slate-200 min-w-0 h-full min-h-0">
        <EvidencePanel run={selectedRun} loading={threadLoading} />
      </aside>

      <DraftTicketModal
        open={ticketModalRunId !== null}
        onClose={() => setTicketModalRunId(null)}
        request={ticketRun?.request ?? ""}
        lane={ticketRun?.lane ?? null}
        spec={ticketRun?.spec ?? null}
      />
    </div>
  );
}
