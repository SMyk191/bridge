import { useState } from "react";
import CorrectThisForm from "./CorrectThisForm";

export interface AnswerFooterProps {
  runId: string;
  hasSpec: boolean;
  votedValue: "up" | "down" | null;
  onVote: (vote: "up" | "down") => void;
  onSubmitCorrection: (term: string, entities: string[], note?: string) => void;
  candidateEntities: string[];
  onCopyPermalink: () => void;
  onDraftTicket: () => void;
}

export default function AnswerFooter(props: AnswerFooterProps) {
  const {
    runId,
    hasSpec,
    votedValue,
    onVote,
    onSubmitCorrection,
    candidateEntities,
    onCopyPermalink,
    onDraftTicket,
  } = props;
  const [correcting, setCorrecting] = useState(false);
  const formId = `correct-this-form-${runId}`;

  function handleCorrectionSubmit(term: string, entities: string[], note?: string) {
    onSubmitCorrection(term, entities, note);
    setCorrecting(false);
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3 text-xs text-slate-500">
        <button
          type="button"
          onClick={() => onVote("up")}
          aria-label="Vote this answer up"
          className={votedValue === "up" ? "font-medium text-indigo-600" : "hover:text-slate-900"}
        >
          👍
        </button>
        <button
          type="button"
          onClick={() => onVote("down")}
          aria-label="Vote this answer down"
          className={votedValue === "down" ? "font-medium text-indigo-600" : "hover:text-slate-900"}
        >
          👎
        </button>
        <button
          type="button"
          onClick={() => setCorrecting((open) => !open)}
          aria-expanded={correcting}
          aria-controls={formId}
          className="hover:text-slate-900"
        >
          Correct this
        </button>
        <button type="button" onClick={onCopyPermalink} className="hover:text-slate-900">
          Copy permalink
        </button>
        <button
          type="button"
          onClick={onDraftTicket}
          disabled={!hasSpec}
          title={
            hasSpec ? undefined : "No technical brief for this run (only change requests and bugs get one)"
          }
          className="rounded-md border border-slate-200 px-2.5 py-1.5 text-xs hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Draft ticket
        </button>
      </div>
      {correcting && (
        <div id={formId}>
          <CorrectThisForm
            candidateEntities={candidateEntities}
            onSubmit={handleCorrectionSubmit}
            onCancel={() => setCorrecting(false)}
          />
        </div>
      )}
    </div>
  );
}
