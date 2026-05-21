import { useCallback, useEffect, useRef, useState } from "react";
import type { TestMatchJob } from "../api/types";
import { useApi } from "../api/client";
import { SimPlayer } from "./SimPlayer";

const WIDE_THRESHOLD = 520;

const STATUS_CLASS: Record<string, string> = {
  success: "ready",
  failure: "failed",
  running: "building",
  queued:  "queued",
};

function formatAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60_000)        return "just now";
  if (diff < 3_600_000)     return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000)    return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

interface Props {
  matchTabs: TestMatchJob[];
  activeTabId: number | null;
  projectId: number | null;
  onTabSelect: (id: number) => void;
  onTabClose: (id: number) => void;
  onOpenMatch: (job: TestMatchJob, newTab: boolean) => void;
}

export function MatchViewer({
  matchTabs,
  activeTabId,
  projectId,
  onTabSelect,
  onTabClose,
  onOpenMatch,
}: Props) {
  const api = useApi();
  const containerRef = useRef<HTMLDivElement>(null);
  const [isWide, setIsWide]         = useState(false);
  const [splitRatio, setSplitRatio] = useState(0.62);
  const [dragging, setDragging]     = useState(false);

  const [showHistory, setShowHistory]   = useState(false);
  const [historyJobs, setHistoryJobs]   = useState<TestMatchJob[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  const activeJob = matchTabs.find((t) => t.id === activeTabId) ?? null;

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

  // ── History fetch ─────────────────────────────────────────────────────────
  const openHistory = async () => {
    if (showHistory) { setShowHistory(false); return; }
    if (!projectId) return;
    setShowHistory(true);
    setHistoryLoading(true);
    try {
      const jobs = await api.listTestMatchJobs(projectId);
      setHistoryJobs(jobs);
    } catch {
      setHistoryJobs([]);
    } finally {
      setHistoryLoading(false);
    }
  };

  // Reset history when project changes
  useEffect(() => {
    setShowHistory(false);
    setHistoryJobs([]);
  }, [projectId]);

  // ── Player content ────────────────────────────────────────────────────────
  const playerContent = (() => {
    if (!activeJob) {
      return (
        <div className="empty">
          <span className="big">no runs yet</span>
          <span style={{ maxWidth: 300 }}>
            Build your agent, then click Test to run a match.
            {historyJobs.length === 0 && projectId && (
              <> Or open a past run from history.</>
            )}
          </span>
        </div>
      );
    }
    if (activeJob.status === "queued") {
      return (
        <div className="empty">
          <span className="big">queued</span>
          <span style={{ maxWidth: 300 }}>Waiting for the test runner…</span>
        </div>
      );
    }
    return <SimPlayer job={activeJob} />;
  })();

  // ── Console content ───────────────────────────────────────────────────────
  const consoleContent = (
    <span className="muted">{`# console\n# run a test match to see output here.`}</span>
  );

  return (
    <div className="panel" ref={containerRef}>
      <div className="panel-head">
        <span className="title">Match Viewer</span>
        {activeJob && (
          <span className={`pill ${STATUS_CLASS[activeJob.status] ?? ""}`}>
            <span className="dot" />
            {activeJob.status}
          </span>
        )}
        <span className="spacer" />
        {projectId && (
          <button
            className="btn ghost"
            style={showHistory ? { borderColor: "var(--accent)" } : undefined}
            onClick={openHistory}
            title="Past runs"
          >
            History
          </button>
        )}
      </div>

      {showHistory && (
        <div className="mv-history-panel">
          <div className="mv-history-title">Past runs</div>
          {historyLoading && <div className="mv-history-empty">Loading…</div>}
          {!historyLoading && historyJobs.length === 0 && (
            <div className="mv-history-empty">No runs yet.</div>
          )}
          {historyJobs.map((job) => (
            <div key={job.id} className="mv-history-row">
              <span className={`dot ${STATUS_CLASS[job.status] ?? ""}`} style={{ flexShrink: 0 }} />
              <span className="mv-history-id">#{job.id}</span>
              <span className="mv-history-time muted">{formatAgo(job.requested_at)}</span>
              <button
                className="btn ghost mv-history-btn"
                title="Open in this tab"
                onClick={() => { onOpenMatch(job, false); setShowHistory(false); }}
              >
                Open
              </button>
              <button
                className="btn ghost mv-history-btn"
                title="Open in new tab"
                onClick={() => { onOpenMatch(job, true); setShowHistory(false); }}
              >
                + Tab
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Tab bar */}
      {matchTabs.length > 0 && (
        <div className="mv-tabs">
          {matchTabs.map((tab) => (
            <button
              key={tab.id}
              className={`mv-tab${activeTabId === tab.id ? " active" : ""}`}
              onClick={() => onTabSelect(tab.id)}
            >
              <span className={`dot ${STATUS_CLASS[tab.status] ?? ""}`} />
              <span>#{tab.id}</span>
              <span
                className="mv-tab-close"
                role="button"
                onClick={(e) => { e.stopPropagation(); onTabClose(tab.id); }}
              >
                ×
              </span>
            </button>
          ))}
        </div>
      )}

      {/* Body: responsive split */}
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
