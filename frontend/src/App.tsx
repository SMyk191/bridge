import { BrowserRouter, Route, Routes } from "react-router-dom";
import Home from "./pages/Home";
import RunView from "./pages/RunView";

export default function App() {
  return (
    <BrowserRouter>
      <div className="h-screen flex flex-col">
        <header className="border-b border-slate-200 px-4 py-2 flex items-center gap-2">
          <span className="font-semibold text-slate-900">Bridge</span>
          <span className="text-xs text-slate-400">
            product-language answers, grounded in the codebase
          </span>
        </header>
        <div className="flex-1 min-h-0">
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/runs/:id" element={<RunView />} />
          </Routes>
        </div>
      </div>
    </BrowserRouter>
  );
}
