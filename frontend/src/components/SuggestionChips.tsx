import type { Suggestion } from "../types";

export interface SuggestionChipsProps {
  suggestions: Suggestion[];
  onPick: (question: string) => void;
  loading: boolean;
}

const SKELETON_WIDTHS = ["w-40", "w-56", "w-32", "w-48"];

export default function SuggestionChips(props: SuggestionChipsProps) {
  if (!props.loading && props.suggestions.length === 0) {
    return null;
  }

  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs text-slate-500">Try asking</span>
      <div className="flex flex-wrap gap-2">
        {props.loading
          ? SKELETON_WIDTHS.map((width, index) => (
              <div
                key={index}
                className={`h-8 ${width} animate-pulse rounded-md bg-slate-100`}
              />
            ))
          : props.suggestions.map((suggestion, index) => (
              <button
                key={index}
                type="button"
                onClick={() => props.onPick(suggestion.question)}
                className="rounded-md border border-slate-200 bg-white px-3 py-1.5 text-left text-sm hover:border-slate-300 hover:bg-slate-50"
              >
                {suggestion.question}
              </button>
            ))}
      </div>
    </div>
  );
}
