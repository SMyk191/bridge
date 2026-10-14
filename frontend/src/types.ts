/**
 * Mirrors the JSON contract served by `backend/bridge/api/main.py` exactly —
 * field names and optionality copied from the Python dataclasses that produce
 * them (`agents/state.py`, `retrieval/retrieve.py`, `orchestrator/store.py`,
 * `agents/llm.py::Usage`). If a backend field is `Optional`/can be `None`,
 * the TS type says `| null` rather than making the field optional, so a
 * missing-vs-null bug shows up as a type error instead of `undefined`
 * silently reading fine.
 */

export type Lane = "question" | "change_request" | "bug" | "data_question" | "changelog";
export type Confidence = "high" | "medium" | "low";
export type RunStatus = "running" | "done" | "aborted" | "needs_clarification" | "error";
export type Intent = "question" | "change_request" | "bug" | "data_question";

// -- evidence -----------------------------------------------------------

export interface ChunkHistory {
  commits_all_time: number;
  commits_recent: number;
  recent_window_days: number;
  last_changed: string | null;
  days_since_change: number | null;
  module: string;
}

/** One cited piece of evidence — `repo@sha:path:start-end` plus the code itself. */
export interface Chunk {
  node_id: string;
  kind: string; // file|class|interface|enum|record|method|endpoint|entity|screen
  name: string;
  path: string;
  start_line: number;
  end_line: number;
  citation: string;
  description: string;
  score: number;
  rerank_score: number | null;
  semantic_score: number | null;
  keyword_score: number | null;
  graph_depth: number | null;
  sources: string[]; // e.g. ["semantic", "graph"]
  graph_path: string | null; // e.g. "PetController -[contains(member)]-> processCreationForm"
  history: ChunkHistory | null;
  code: string;
}

// -- impact map -----------------------------------------------------------

export interface ImpactEntry {
  name: string;
  citation: string;
  why: string;
  node_id: string;
}

export const IMPACT_MAP_KINDS = [
  "endpoints",
  "classes_methods",
  "entities_tables",
  "screens",
  "hubs_crossed",
] as const;
export type ImpactMapKind = (typeof IMPACT_MAP_KINDS)[number];

export interface ImpactMap {
  summary: string;
  endpoints: ImpactEntry[];
  classes_methods: ImpactEntry[];
  entities_tables: ImpactEntry[];
  screens: ImpactEntry[];
  hubs_crossed: ImpactEntry[];
  modules: string[];
  /** Computed by rules over the evidence, never taken from a model. */
  confidence: Confidence;
  /** Every rule that decided the level, in the order it fired. */
  confidence_reasons: string[];
  /** What the cartographer claimed about itself. Recorded, never acted on. */
  model_confidence: Confidence | "";
  gaps: string[];
  module_summaries: Record<string, string>;
  total: number;
}

// -- verification -----------------------------------------------------------

export interface DroppedCitation {
  citation: string;
  reason: string;
}

export interface VerificationPass {
  checked: number;
  verified: number;
  dropped: DroppedCitation[];
  skipped: number;
  drop_ratio: number;
}

export interface ConfidenceVerdict {
  level: Confidence;
  /** Why it is not higher — the audit trail for a deterministic score. */
  reasons: string[];
  /** Phrases fed into the answer's "What would make me more sure" line. */
  missing: string[];
}

export interface Verification {
  answer: VerificationPass;
  impact_map: VerificationPass;
  confidence: ConfidenceVerdict;
}

// -- trace / usage -----------------------------------------------------------

export interface TraceEntry {
  node: string;
  status: "ok" | "skipped" | "error" | "budget_exceeded" | "running";
  elapsed_s: number;
  detail: string;
  calls: number;
  cost_usd: number;
  input_tokens: number;
}

export interface Usage {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  thinking_tokens: number;
  cost_usd: number;
  by_role: Record<string, number>;
  by_provider: Record<string, number>;
  errors: number;
}

// -- runs -----------------------------------------------------------

/** `GET /runs/{id}` — the full, persisted run. */
export interface RunDetail {
  run_id: string;
  thread_id: string;
  request: string;
  status: RunStatus;
  lane: Lane | null;
  abort_reason: string | null;
  answer: string;
  impact_map: ImpactMap | null;
  /** The technical brief — architect lanes (change_request, bug) only. */
  spec: string | null;
  /** The lane writer's prose: the explainer's walkthrough, or the changelog. */
  narrative: string | null;
  /** What checking the answer's citations against the graph turned up. */
  verification: Verification | null;
  evidence: Chunk[];
  trace: TraceEntry[];
  usage: Usage | null;
  sha: string;
  env_label: string; // "indexed at <short sha>, <date>"
  created_at: string;
  updated_at: string;
}

/** One row of `GET /threads/{id}.runs[]` — summary only, no `state` blob. */
export interface RunSummary {
  run_id: string;
  thread_id: string;
  request: string;
  sha: string | null;
  lane: Lane | null;
  status: RunStatus;
  confidence: Confidence | null;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  elapsed_s: number;
  citations: number;
  created_at: string;
  updated_at: string;
}

export interface ThreadSummary {
  thread_id: string;
  run_count: number;
  latest_run_id: string;
  latest_request: string;
  latest_status: RunStatus;
  latest_lane: Lane | null;
  latest_confidence: Confidence | null;
  updated_at: string;
}

export interface ThreadDetail {
  thread_id: string;
  runs: RunSummary[];
}

// -- feedback / vocab -----------------------------------------------------------

export type Vote = "up" | "down";

export interface FeedbackResponse {
  id: number;
  run_id: string;
  vote: Vote;
  note: string | null;
}

export interface VocabProposalResponse {
  id: number;
  term: string;
  entities: string[];
  note: string | null;
  status: string; // always "pending" fresh out of the API — approval is manual
}

// -- suggestions / health -----------------------------------------------------------

export interface Suggestion {
  question: string;
  kind: string | null;
  node_id: string | null;
}

export interface SuggestionsResponse {
  sha: string;
  questions: Suggestion[];
  source: string | null;
  cost_usd?: number;
  note?: string; // present when no suggestions.json exists yet for this index
}

export interface HealthResponse {
  status: string;
  repo_name: string | null;
  sha: string;
  indexed_at: string | null;
  age_hours: number | null;
  nodes: Record<string, number>;
  edges: Record<string, number>;
  stale: boolean;
  current_head_sha: string | null;
}

// -- SSE stream (POST /runs) -----------------------------------------------------------
//
// The API's 5-type contract (api/main.py::_sse_frame / _stream_run). Each frame's
// JSON body carries `type` plus whatever the originating orchestrator event added —
// which node emitted it and which extra keys are present varies by node, so those
// are typed loosely (`Record<string, unknown>`) rather than enumerated per node.

export interface ProgressEvent {
  type: "progress";
  node: string; // "" for the run_started frame, else interpreter|cartographer|historian|architect
  detail: string;
  [extra: string]: unknown;
}

export interface FindingEvent {
  type: "finding";
  node: string;
  detail: string;
  impact_map?: ImpactMap;
  [extra: string]: unknown;
}

export interface AnswerEvent {
  type: "answer";
  run_id: string;
  answer: string;
  citations: string[];
  confidence: Confidence | null;
}

export interface ErrorEvent {
  type: "error";
  node: string;
  detail: string;
}

export interface DoneEvent {
  type: "done";
  run_id: string | null;
  thread_id: string | null;
  status: RunStatus;
  abort_reason?: string | null;
  lane?: Lane | null;
  citations?: number;
  cost_usd?: number;
  elapsed_s?: number | null;
  permalink?: string | null;
  /** Served from a previous run's answer rather than freshly computed — see
   * the answer's own header for which run and when. */
  cache_hit?: boolean;
}

export type RunEvent = ProgressEvent | FindingEvent | AnswerEvent | ErrorEvent | DoneEvent;
