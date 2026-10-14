import { useState } from "react";

export interface CorrectThisFormProps {
  candidateEntities: string[];
  onSubmit: (term: string, entities: string[], note?: string) => void;
  onCancel: () => void;
}

export default function CorrectThisForm(props: CorrectThisFormProps) {
  const { candidateEntities, onSubmit, onCancel } = props;
  const [term, setTerm] = useState("");
  const [note, setNote] = useState("");
  const [checkedEntities, setCheckedEntities] = useState<string[]>([]);

  function toggleEntity(entity: string) {
    setCheckedEntities((prev) =>
      prev.includes(entity) ? prev.filter((e) => e !== entity) : [...prev, entity],
    );
  }

  const canSubmit = term.trim().length > 0;

  function handleSubmit() {
    if (!canSubmit) return;
    onSubmit(term.trim(), checkedEntities, note.trim() || undefined);
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-slate-200 bg-slate-50 p-3">
      <input
        type="text"
        value={term}
        onChange={(e) => setTerm(e.target.value)}
        placeholder="The correct product term..."
        className="w-full rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-1 focus:ring-indigo-600"
      />
      {candidateEntities.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1.5">
          {candidateEntities.map((entity) => (
            <label key={entity} className="flex items-center gap-1.5 text-xs text-slate-700">
              <input
                type="checkbox"
                checked={checkedEntities.includes(entity)}
                onChange={() => toggleEntity(entity)}
                className="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-600"
              />
              <span className="font-mono">{entity}</span>
            </label>
          ))}
        </div>
      )}
      <textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Why this correction? (optional)"
        rows={2}
        className="w-full rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-1 focus:ring-indigo-600"
      />
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={handleSubmit}
          disabled={!canSubmit}
          className="rounded-md border border-transparent bg-indigo-600 px-2.5 py-1.5 text-xs text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Submit
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-slate-200 px-2.5 py-1.5 text-xs hover:bg-slate-50"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
