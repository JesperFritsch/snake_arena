import { useCallback, useEffect, useRef, useState } from "react";
import type { BuildJob, TestMatchJob } from "../api/types";
import { SimPlayer } from "./SimPlayer";

// Below this pixel width the layout stacks vertically (player above console).
// Above it the layout goes horizontal (player left, console right).
const WIDE_THRESHOLD = 520;

const STATUS_CLASS: Record<string, string> = {
  success: "ready",
  failure: "failed",
  running: "building",
  queued:  "queued",
};

interface Props {
  buildJob: BuildJob | null;
  testMatchJob: TestMatchJob | null;
}

export function MatchViewer({ buildJob, testMatchJob }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [isWide, setIsWide]         = useState(false);
  const [splitRatio, setSplitRatio] = useState(0.62);
  const [dragging, setDragging]     = useState(false);

  // ── Watch container width ─────────────────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const obs = new ResizeObserver(([entry]) => {
      setIsWide(entry.contentRect.width >= WIDE_THRESHOLD);
    });
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  // ── Custom resize handle drag ─────────────────────────────────────────────
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setDragging(true);
  }, []);

  useEffect(() => {
    if (!dragging) return;

    const onMove = (e: MouseEvent) => {
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const raw  = isWide
        ? (e.clientX - rect.left)  / rect.width
        : (e.clientY - rect.top)   / rect.height;
      setSplitRatio(Math.min(0.85, Math.max(0.15, raw)));
    };

    const onUp = () => setDragging(false);
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup",   onUp);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup",   onUp);
    };
  }, [dragging, isWide]);

  useEffect(() => {
    if (!dragging) return;
    document.body.style.cursor     = isWide ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";
    return () => {
      document.body.style.cursor     = "";
      document.body.style.userSelect = "";
    };
  }, [dragging, isWide]);

  // ── Player content ────────────────────────────────────────────────────────
  const playerContent = (() => {
    if (!testMatchJob) {
      return (
        <div className="empty">
          <span className="big">no runs yet</span>
          <span style={{ maxWidth: 300 }}>
            Build your agent, then click Test to run a match.
          </span>
        </div>
      );
    }
    if (testMatchJob.status === "queued") {
      return (
        <div className="empty">
          <span className="big">queued</span>
          <span style={{ maxWidth: 300 }}>Waiting for the test runner…</span>
        </div>
      );
    }
    return <SimPlayer jobId={testMatchJob.id} />;
  })();

  // ── Console content ───────────────────────────────────────────────────────
  const consoleContent = buildJob?.status === "failure" && buildJob.error ? (
    <>
      <span className="muted">{`# build #${buildJob.id} failed\n`}</span>
      <span className="err">{buildJob.error}</span>
    </>
  ) : buildJob ? (
    <span className="muted">
      {`# build #${buildJob.id} — ${buildJob.status}\n`}
      {`# compilation logs will appear here once the API exposes them.`}
    </span>
  ) : (
    <span className="muted">{`# console\n# build or run a snake to see output here.`}</span>
  );

  return (
    <div className="panel" ref={containerRef}>
      <div className="panel-head">
        <span className="title">Match Viewer</span>
        {testMatchJob && (
          <span className={`pill ${STATUS_CLASS[testMatchJob.status] ?? ""}`}>
            <span className="dot" />
            {testMatchJob.status}
          </span>
        )}
      </div>

      {/* Body: responsive split — never unmounts either pane */}
      <div
        className="mv-body"
        style={{ flexDirection: isWide ? "row" : "column" }}
      >
        <div className="mv-pane" style={{ flex: splitRatio }}>
          {playerContent}
        </div>

        <div
          className="resize-handle"
          data-panel-group-direction={isWide ? "horizontal" : "vertical"}
          style={{ cursor: isWide ? "col-resize" : "row-resize", flexShrink: 0 }}
          onMouseDown={handleMouseDown}
        />

        <div
          className="mv-pane mv-console"
          style={{ flex: 1 - splitRatio }}
        >
          <div className="console">{consoleContent}</div>
        </div>
      </div>
    </div>
  );
}
