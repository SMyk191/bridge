import { useEffect, useRef } from "react";
import type { Lane } from "../types";
import Markdown from "./Markdown";

export interface DraftTicketModalProps {
  open: boolean;
  onClose: () => void;
  request: string;
  lane: Lane | null;
  spec: string | null;
}

const LANE_STYLES: Record<Lane, string> = {
  question: "bg-slate-100 text-slate-700",
  change_request: "bg-blue-50 text-blue-700",
  bug: "bg-rose-50 text-rose-700",
  data_question: "bg-amber-50 text-amber-700",
  changelog: "bg-violet-50 text-violet-700",
};

export default function DraftTicketModal(props: DraftTicketModalProps) {
  const { open, onClose, request, lane, spec } = props;
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    closeButtonRef.current?.focus();
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 flex items-center justify-center bg-slate-900/40 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="draft-ticket-title"
        className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="text-xs font-medium uppercase tracking-wide text-slate-400">
              Draft ticket
            </div>
            <h2 id="draft-ticket-title" className="mt-1 text-lg font-semibold text-slate-900">
              {request}
            </h2>
            {lane && (
              <span className={`mt-2 inline-block rounded-md px-1.5 py-0.5 text-xs ${LANE_STYLES[lane]}`}>
                {lane.replace(/_/g, " ")}
              </span>
            )}
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 rounded-md px-2 py-1 text-slate-400 hover:bg-slate-50 hover:text-slate-600"
          >
            ✕
          </button>
        </div>

        <div className="my-4 border-t border-slate-100" />

        {spec ? (
          <Markdown>{spec}</Markdown>
        ) : (
          <p className="text-sm italic text-slate-400">
            No technical brief is available for this run.
          </p>
        )}
      </div>
    </div>
  );
}
