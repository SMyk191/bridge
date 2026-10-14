import type { ReactNode } from "react";
import Markdown from "./Markdown";

export interface MessageBubbleProps {
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
  selected?: boolean;
  onClick?: () => void;
  header?: ReactNode;
  footer?: ReactNode;
}

export default function MessageBubble(props: MessageBubbleProps) {
  const { role, content, streaming, selected, onClick, header, footer } = props;

  if (role === "user") {
    return (
      <div>
        <div className="text-xs font-medium tracking-wide uppercase text-slate-400">
          Question
        </div>
        <div className="mt-1 text-base text-slate-900">{content}</div>
      </div>
    );
  }

  const isThinking = streaming && content.trim().length < 4;

  return (
    <div
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
      className={`rounded-lg border p-4 ${
        selected
          ? "border-indigo-300 bg-indigo-50/30"
          : `border-slate-200 ${onClick ? "hover:border-slate-300" : ""}`
      } ${onClick ? "cursor-pointer" : ""}`}
    >
      {header}
      {isThinking ? (
        <div className="text-slate-400">Thinking...</div>
      ) : (
        <Markdown>{content}</Markdown>
      )}
      {footer && (
        <div className="mt-3 border-t border-slate-100 pt-3">{footer}</div>
      )}
    </div>
  );
}
