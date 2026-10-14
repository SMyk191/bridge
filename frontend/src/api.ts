import type {
  FeedbackResponse,
  HealthResponse,
  RunDetail,
  RunEvent,
  SuggestionsResponse,
  ThreadDetail,
  ThreadSummary,
  Vote,
} from "./types";

// Vite only inlines `import.meta.env.VITE_*` when referenced as a static
// property access, so this can't be pulled into a helper — must stay inline.
const API_BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body?.detail ?? detail;
    } catch {
      // body wasn't JSON — fall back to statusText
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

/**
 * Stream one run. `POST /runs` returns `text/event-stream`; this reads the
 * response body as raw bytes (not `EventSource`, which can't POST a body) and
 * yields one parsed `RunEvent` per SSE frame as soon as it arrives.
 *
 * SSE frames are separated by a blank line; a single fetch chunk can split a
 * frame across two `read()`s (or bundle several frames into one), so this
 * buffers decoded text and only emits on complete `\n\n`-terminated frames.
 */
export async function* streamRun(
  question: string,
  threadId: string | null,
  signal?: AbortSignal,
): AsyncGenerator<RunEvent, void, void> {
  const response = await fetch(`${API_BASE}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, thread_id: threadId ?? undefined }),
    signal,
  });
  if (!response.ok || !response.body) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body?.detail ?? detail;
    } catch {
      // ignore — use statusText
    }
    throw new ApiError(response.status, detail);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseSseFrame(frame);
        if (event) yield event;
        boundary = buffer.indexOf("\n\n");
      }
    }
    // a final frame without a trailing blank line (stream closed mid-frame)
    const trailing = parseSseFrame(buffer);
    if (trailing) yield trailing;
  } finally {
    reader.releaseLock();
  }
}

function parseSseFrame(frame: string): RunEvent | null {
  for (const line of frame.split("\n")) {
    if (line.startsWith("data:")) {
      const raw = line.slice(5).trimStart();
      if (!raw) continue;
      try {
        return JSON.parse(raw) as RunEvent;
      } catch {
        return null; // a malformed frame is dropped, not fatal to the stream
      }
    }
  }
  return null;
}

export async function getRun(runId: string): Promise<RunDetail> {
  const response = await fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}`);
  return asJson<RunDetail>(response);
}

export async function listThreads(limit = 50): Promise<ThreadSummary[]> {
  const response = await fetch(`${API_BASE}/threads?limit=${limit}`);
  const body = await asJson<{ threads: ThreadSummary[] }>(response);
  return body.threads;
}

export async function getThread(threadId: string): Promise<ThreadDetail> {
  const response = await fetch(`${API_BASE}/threads/${encodeURIComponent(threadId)}`);
  return asJson<ThreadDetail>(response);
}

export async function postFeedback(
  runId: string,
  vote: Vote,
  note?: string,
): Promise<FeedbackResponse> {
  const response = await fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vote, note: note || undefined }),
  });
  return asJson<FeedbackResponse>(response);
}

export async function postVocabProposal(
  term: string,
  entities: string[],
  note?: string,
): Promise<void> {
  const response = await fetch(`${API_BASE}/vocab/proposals`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ term, entities, note: note || undefined }),
  });
  await asJson(response);
}

export async function getSuggestions(): Promise<SuggestionsResponse> {
  const response = await fetch(`${API_BASE}/suggestions`);
  return asJson<SuggestionsResponse>(response);
}

export async function getHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE}/health`);
  return asJson<HealthResponse>(response);
}

export function runPermalink(runId: string): string {
  return `${window.location.origin}/runs/${runId}`;
}
