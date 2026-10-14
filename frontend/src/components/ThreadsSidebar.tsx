import type { Lane, Suggestion, ThreadSummary } from "../types";
import SuggestionChips from "./SuggestionChips";

export interface ThreadsSidebarProps {
  threads: ThreadSummary[];
  activeThreadId: string | null;
  onSelectThread: (threadId: string) => void;
  onNewThread: () => void;
  loading: boolean;
  suggestions: Suggestion[];
  suggestionsLoading: boolean;
  showSuggestions: boolean;
  onPickSuggestion: (question: string) => void;
}

const LANE_STYLES: Record<Lane, string> = {
  question: "bg-slate-100 text-slate-700",
  change_request: "bg-blue-50 text-blue-700",
  bug: "bg-rose-50 text-rose-700",
  data_question: "bg-amber-50 text-amber-700",
  changelog: "bg-violet-50 text-violet-700",
};

const LANE_LABELS: Record<Lane, string> = {
  question: "Question",
  change_request: "Change request",
  bug: "Bug",
  data_question: "Data question",
  changelog: "What changed",
};

function formatTimestamp(iso: string): string {
  const [date, time] = iso.split("T");
  if (!date || !time) return iso;
  return `${date} ${time.slice(0, 5)}`;
}

export default function ThreadsSidebar(props: ThreadsSidebarProps) {
  const {
    threads,
    activeThreadId,
    onSelectThread,
    onNewThread,
    loading,
    suggestions,
    suggestionsLoading,
    showSuggestions,
    onPickSuggestion,
  } = props;

  return (
    <div className="flex h-full flex-col">
      <div className="p-3">
        <button
          type="button"
          onClick={onNewThread}
          className="w-full rounded-md border border-transparent bg-indigo-600 px-2.5 py-1.5 text-sm text-white hover:bg-indigo-500"
        >
          New thread
        </button>
      </div>
      <div className="flex-1 overflow-y-auto px-2 pb-3">
        {loading ? (
          <p className="px-2 py-3 text-xs text-slate-500">Loading threads...</p>
        ) : threads.length === 0 ? (
          <p className="px-2 py-3 text-xs text-slate-500">
            No threads yet. Ask a question to start one.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {threads.map((thread) => {
              const isActive = thread.thread_id === activeThreadId;
              return (
                <li key={thread.thread_id}>
                  <button
                    type="button"
                    onClick={() => onSelectThread(thread.thread_id)}
                    className={`w-full rounded-lg border p-3 text-left ${
                      isActive
                        ? "border-slate-200 border-l-2 border-l-indigo-600 bg-indigo-50 text-indigo-700"
                        : "border-slate-200 hover:bg-slate-50"
                    }`}
                  >
                    <p className="line-clamp-2 text-sm">{thread.latest_request}</p>
                    <div className="mt-2 flex items-center gap-2">
                      {thread.latest_lane && (
                        <span
                          className={`rounded-md px-1.5 py-0.5 text-xs ${LANE_STYLES[thread.latest_lane]}`}
                        >
                          {LANE_LABELS[thread.latest_lane]}
                        </span>
                      )}
                      <span className={`text-xs ${isActive ? "text-indigo-700/70" : "text-slate-400"}`}>
                        {thread.run_count} {thread.run_count === 1 ? "run" : "runs"} ·{" "}
                        {formatTimestamp(thread.updated_at)}
                      </span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {showSuggestions && (
        <div className="border-t border-slate-200 p-3">
          <SuggestionChips
            suggestions={suggestions}
            onPick={onPickSuggestion}
            loading={suggestionsLoading}
          />
        </div>
      )}
    </div>
  );
}
