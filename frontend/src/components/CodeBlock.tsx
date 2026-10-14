export interface CodeBlockProps {
  code: string;
  startLine: number;
}

export default function CodeBlock(props: CodeBlockProps) {
  const lines = props.code.split("\n");

  return (
    <div className="rounded-md border border-slate-200 border-l-2 border-l-indigo-400 bg-slate-50 font-mono text-xs overflow-x-auto">
      {lines.map((line, index) => (
        <div key={index} className="flex">
          <span className="w-10 shrink-0 select-none text-right pr-3 text-slate-400">
            {props.startLine + index}
          </span>
          <span className="whitespace-pre">{line}</span>
        </div>
      ))}
    </div>
  );
}
