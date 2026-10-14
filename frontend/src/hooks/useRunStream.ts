import { useCallback, useEffect, useRef, useState } from "react";
import { streamRun } from "../api";
import type { Confidence, ImpactMap, Lane, RunEvent, RunStatus } from "../types";
import type { ProgressStage } from "../components/ProgressRail";

/**
 * The orchestrator's real node sequence (orchestrator/graph.py::NODE_SEQUENCE).
 * "architect" only actually runs for change_request/bug lanes — it starts
 * "pending" and is flipped to "skipped" the moment the lane_decided event says
 * otherwise, so the rail visibly reflects design law 3 (lane is decided AFTER
 * cartographer, not from how the question was phrased).
 */
const STAGE_ORDER: { key: string; label: string }[] = [
  { key: "interpreter", label: "Interpreting" },
  { key: "cartographer", label: "Mapping the codebase" },
  { key: "historian", label: "Checking history" },
  { key: "architect", label: "Drafting brief" },
];

function initialStages(): ProgressStage[] {
  return STAGE_ORDER.map((s) => ({ ...s, status: "pending" as const }));
}

export interface RunStreamState {
  status: "idle" | "streaming" | "done" | "error";
  stages: ProgressStage[];
  answer: string;
  citations: string[];
  confidence: Confidence | null;
  runId: string | null;
  threadId: string | null;
  lane: Lane | null;
  costUsd: number | null;
  elapsedS: number | null;
  permalink: string | null;
  impactMap: ImpactMap | null;
  runStatus: RunStatus | null;
  errorDetail: string | null;
}

function initialState(): RunStreamState {
  return {
    status: "idle",
    stages: initialStages(),
    answer: "",
    citations: [],
    confidence: null,
    runId: null,
    threadId: null,
    lane: null,
    costUsd: null,
    elapsedS: null,
    permalink: null,
    impactMap: null,
    runStatus: null,
    errorDetail: null,
  };
}

function markStage(
  stages: ProgressStage[],
  key: string,
  status: ProgressStage["status"],
  detail?: string,
): ProgressStage[] {
  return stages.map((s) => (s.key === key ? { ...s, status, detail: detail ?? s.detail } : s));
}

type ProgressEventShape = Extract<RunEvent, { type: "progress" }>;
type DoneEventShape = Extract<RunEvent, { type: "done" }>;

function applyProgress(prev: RunStreamState, event: ProgressEventShape): RunStreamState {
  const { node, detail } = event;

  // run_started — the one progress frame with no node of its own.
  if (node === "") {
    return {
      ...prev,
      runId: (event.run_id as string | undefined) ?? prev.runId,
      threadId: (event.thread_id as string | undefined) ?? prev.threadId,
    };
  }

  // lane_decided rides on the same "node" as whichever stage just finished,
  // distinguished only by carrying a `lane` field — everything else on a
  // progress frame never has one.
  if (typeof event.lane === "string") {
    const lane = event.lane as Lane;
    const stages =
      lane === "question" || lane === "data_question"
        ? markStage(prev.stages, "architect", "skipped")
        : prev.stages;
    return { ...prev, lane, stages };
  }

  if (detail === "") {
    return { ...prev, stages: markStage(prev.stages, node, "active") };
  }

  return { ...prev, stages: markStage(prev.stages, node, "done", detail) };
}

function finalizeDone(prev: RunStreamState, event: DoneEventShape): RunStreamState {
  // Anything still pending/active when the stream ends genuinely never ran
  // (needs_clarification and the data_question redirect both short-circuit
  // before later nodes) — done nodes were already marked "done" per-event.
  const stages = prev.stages.map((s) =>
    s.status === "pending" || s.status === "active" ? { ...s, status: "skipped" as const } : s,
  );
  return {
    ...prev,
    status: event.status === "error" ? "error" : "done",
    runStatus: event.status,
    runId: event.run_id ?? prev.runId,
    threadId: event.thread_id ?? prev.threadId,
    lane: (event.lane ?? prev.lane) ?? null,
    costUsd: event.cost_usd ?? prev.costUsd,
    elapsedS: event.elapsed_s ?? prev.elapsedS,
    permalink: event.permalink ?? prev.permalink,
    stages,
  };
}

function applyEvent(prev: RunStreamState, event: RunEvent): RunStreamState {
  switch (event.type) {
    case "progress":
      return applyProgress(prev, event);
    case "finding":
      return { ...prev, impactMap: event.impact_map ?? prev.impactMap };
    case "answer":
      return {
        ...prev,
        answer: event.answer,
        citations: event.citations,
        confidence: event.confidence,
      };
    case "error":
      return {
        ...prev,
        errorDetail: event.detail,
        stages: markStage(prev.stages, event.node, "error"),
      };
    case "done":
      return finalizeDone(prev, event);
    default:
      return prev;
  }
}

/**
 * Drives one streamed POST /runs call and reduces its SSE events into render-
 * ready state. `onDone` fires once, after the stream's terminal "done" event
 * has already been folded into state — the caller uses it to refresh threads/
 * evidence rather than polling.
 */
export function useRunStream(onDone?: (finalState: RunStreamState) => void) {
  const [state, setState] = useState<RunStreamState>(initialState);
  const abortRef = useRef<AbortController | null>(null);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  useEffect(() => () => abortRef.current?.abort(), []);

  const start = useCallback((question: string, threadId: string | null) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setState({ ...initialState(), status: "streaming", threadId });

    (async () => {
      let receivedTerminal = false;
      try {
        for await (const event of streamRun(question, threadId, controller.signal)) {
          if (event.type === "done") receivedTerminal = true;
          setState((prev) => applyEvent(prev, event));
        }
        if (!receivedTerminal) {
          // The connection closed (server crash, network drop) without ever
          // sending a "done" frame — without this, status stays "streaming"
          // forever: the composer stays disabled and nothing tells the user
          // the run didn't actually finish.
          setState((prev) => ({
            ...prev,
            status: "error",
            errorDetail: "Connection closed before the run finished.",
          }));
          return;
        }
        setState((prev) => {
          onDoneRef.current?.(prev);
          return prev;
        });
      } catch (err) {
        if (controller.signal.aborted) return;
        setState((prev) => ({
          ...prev,
          status: "error",
          errorDetail: err instanceof Error ? err.message : String(err),
        }));
      }
    })();
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setState(initialState());
  }, []);

  return { state, start, reset };
}
