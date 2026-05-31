import React, { useCallback, useEffect, useRef, useState } from "react";
import type { TestMatchJob } from "../api/types";
import { useApi } from "../api/client";
import { LiveSimPlayer } from "./LiveSimPlayer";

const WIDE_THRESHOLD = 520;
const MAX_PINNED = 9;
const TERMINAL = new Set(["success", "failure", "cancelled"]);

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

function matchLabel(job: TestMatchJob): string {
  if (job.status === "queued" || job.status === "running") return "#?";
  return `#${job.match_number ?? job.id}`;
}

interface Props {
  matchTabs: TestMatchJob[];
  activeTabId: number | null;
  projectId: number | null;
  onTabSelect: (id: number) => void;
  onTabClose: (id: number) => void;
  onOpenMatch: (job: TestMatchJob, newTab: boolean) => void;
  onMatchStatus: (jobId: number, status: string) => void;
  onBuildStatus: (status: string) => void;
  onJobPinChange?: (job: TestMatchJob) => void;
  onJobsRefreshed?: (jobs: TestMatchJob[]) => void;
}

export function MatchViewer({
  matchTabs,
  activeTabId,
  projectId,
  onTabSelect,
  onTabClose,
  onOpenMatch,
  onMatchStatus,
  onBuildStatus,
  onJobPinChange,
  onJobsRefreshed,
}: Props) {
  const api = useApi();
  const containerRef = useRef<HTMLDivElement>(null);
  const [isWide, setIsWide]         = useState(false);
  const [splitRatio, setSplitRatio] = useState(0.62);
  const [dragging, setDragging]     = useState(false);

  const [showHistory, setShowHistory]   = useState(false);
  const [historyJobs, setHistoryJobs]   = useState<TestMatchJob[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [pinBusy, setPinBusy]           = useState<number | null>(null);
  const [cancelBusy, setCancelBusy]     = useState(false);
  const [consoleLog, setConsoleLog]     = useState<string | null>(null);
  const [execTimes, setExecTimes]       = useState<Record<string, number> | null>(null);

  const activeJob = matchTabs.find((t) => t.id === activeTabId) ?? null;
  const pinnedCount = historyJobs.filter((j) => j.pinned).length;

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
      const jobs = await api.listTestMatchJobs(projectId, 50);
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

  // Reset console and exec times when the active tab changes.
  useEffect(() => {
    setConsoleLog(null);
    setExecTimes(null);
  }, [activeTabId]);

  // ── History refresh on terminal status ───────────────────────────────────
  const handleJobStatus = useCallback((jobId: number, newStatus: string) => {
    onMatchStatus(jobId, newStatus);
    if (!TERMINAL.has(newStatus) || !projectId) return;
    api.listTestMatchJobs(projectId, 50).then((freshJobs) => {
      setHistoryJobs(freshJobs);
      onJobsRefreshed?.(freshJobs);
    }).catch(() => {});
  }, [onMatchStatus, projectId, api, onJobsRefreshed]);

  // Close tabs whose jobs were just pruned from history. Tracks the previous
  // history snapshot so we close *only* tabs that disappeared in this refresh
  // — not tabs whose jobs were never in `historyJobs` to begin with (e.g. a
  // tab for the just-completed match before its first appearance in history).
  const prevHistoryIdsRef = useRef<Set<number>>(new Set());
  useEffect(() => {
    const newIds = new Set(historyJobs.map((j) => j.id));
    const prevIds = prevHistoryIdsRef.current;
    prevHistoryIdsRef.current = newIds;
    for (const tab of matchTabs) {
      if (prevIds.has(tab.id) && !newIds.has(tab.id)) {
        onTabClose(tab.id);
      }
    }
  }, [historyJobs]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Cancel queued job ─────────────────────────────────────────────────────
  const handleCancel = async () => {
    if (!activeJob || cancelBusy) return;
    setCancelBusy(true);
    try {
      await api.cancelTestMatch(activeJob.id);
      onMatchStatus(activeJob.id, "cancelled");
    } catch {
      // already started or gone — status update will arrive via WS anyway
    } finally {
      setCancelBusy(false);
    }
  };

  // ── Pin toggle ────────────────────────────────────────────────────────────
  const handlePinToggle = async (job: TestMatchJob) => {
    if (pinBusy !== null) return;
    const newPinned = !job.pinned;
    if (newPinned && pinnedCount >= MAX_PINNED) return;
    setPinBusy(job.id);
    try {
      const updated = await api.pinTestMatch(job.id, newPinned);
      setHistoryJobs((prev) => {
        const next = prev.map((j) => (j.id === updated.id ? updated : j));
        // Re-sort: pinned first, then by recency
        return [...next].sort((a, b) => {
          if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
          return new Date(b.requested_at).getTime() - new Date(a.requested_at).getTime();
        });
      });
      onJobPinChange?.(updated);
    } catch {
      // silent — API returns 422 if limit is hit
    } finally {
      setPinBusy(null);
    }
  };

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
    // Mount LiveSimPlayer for queued/running jobs too — it owns the WebSocket
    // that drives status updates, so it must connect even before the match
    // starts.
    return (
      <LiveSimPlayer
        job={activeJob}
        onConsoleLog={setConsoleLog}
        onExecTimes={setExecTimes}
        onJobStatus={(s) => handleJobStatus(activeJob.id, s)}
        onBuildStatus={onBuildStatus}
      />
    );
  })();

  // ── Console content ───────────────────────────────────────────────────────
  let consoleContent: React.ReactNode;
  if (consoleLog) {
    consoleContent = <pre className="console-pre">{consoleLog}</pre>;
  } else if (activeJob?.status === "failure" && activeJob.error) {
    consoleContent = <pre className="console-pre console-pre-err">{activeJob.error}</pre>;
  } else if (!activeJob) {
    consoleContent = <span className="muted">run a test match to see output here</span>;
  } else {
    consoleContent = <span className="muted">no output for this step</span>;
  }

  // Split history into pinned/unpinned sections for the panel
  const pinnedJobs   = historyJobs.filter((j) => j.pinned);
  const unpinnedJobs = historyJobs.filter((j) => !j.pinned);

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
        {activeJob?.status === "queued" && (
          <button
            className="btn ghost danger"
            disabled={cancelBusy}
            onClick={handleCancel}
          >
            {cancelBusy ? "Cancelling…" : "Cancel"}
          </button>
        )}
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
          {historyLoading && <div className="mv-history-empty">Loading…</div>}
          {!historyLoading && historyJobs.length === 0 && (
            <div className="mv-history-empty">No runs yet.</div>
          )}

          {!historyLoading && pinnedJobs.length > 0 && (
            <>
              <div className="mv-history-section-label">Pinned</div>
              {pinnedJobs.map((job) => (
                <HistoryRow
                  key={job.id}
                  job={job}
                  pinBusy={pinBusy}
                  canPin={pinnedCount < MAX_PINNED}
                  onOpen={(newTab) => { onOpenMatch(job, newTab); setShowHistory(false); }}
                  onPinToggle={() => handlePinToggle(job)}
                />
              ))}
            </>
          )}

          {!historyLoading && unpinnedJobs.length > 0 && (
            <>
              {pinnedJobs.length > 0 && (
                <div className="mv-history-section-label">Recent</div>
              )}
              {unpinnedJobs.map((job) => (
                <HistoryRow
                  key={job.id}
                  job={job}
                  pinBusy={pinBusy}
                  canPin={pinnedCount < MAX_PINNED}
                  onOpen={(newTab) => { onOpenMatch(job, newTab); setShowHistory(false); }}
                  onPinToggle={() => handlePinToggle(job)}
                />
              ))}
            </>
          )}
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
              <span>{matchLabel(tab)}</span>
              {tab.pinned && <span className="mv-tab-pin" title="Pinned">·</span>}
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
          {execTimes && execTimes["0"] !== undefined && (
            <div className="exec-times-bar">
              <span className="exec-times-label">Dev CPU time</span>
              <span className="exec-times-entry">
                <span className="exec-times-name">
                  {activeJob?.participant_names[0] ?? "snake 0"} (dev)
                </span>
                <span className="exec-times-ms">{execTimes["0"].toFixed(3)}ms</span>
              </span>
            </div>
          )}
          <div className="console">{consoleContent}</div>
        </div>
      </div>
    </div>
  );
}

// ── HistoryRow ────────────────────────────────────────────────────────────────

interface HistoryRowProps {
  job: TestMatchJob;
  pinBusy: number | null;
  canPin: boolean;
  onOpen: (newTab: boolean) => void;
  onPinToggle: () => void;
}

function HistoryRow({ job, pinBusy, canPin, onOpen, onPinToggle }: HistoryRowProps) {
  const busy = pinBusy === job.id;
  const pinDisabled = busy || (!job.pinned && !canPin);

  return (
    <div className={`mv-history-row${job.pinned ? " pinned" : ""}`}>
      <span className={`dot ${STATUS_CLASS[job.status] ?? ""}`} style={{ flexShrink: 0 }} />
      <span className="mv-history-id">{`#${job.match_number ?? job.id}`}</span>
      <span className="mv-history-runid">{job.id}</span>
      <span className="mv-history-time muted">{formatAgo(job.requested_at)}</span>
      <button
        className="btn ghost mv-history-btn"
        title="Open in this tab"
        onClick={() => onOpen(false)}
      >
        Open
      </button>
      <button
        className="btn ghost mv-history-btn"
        title="Open in new tab"
        onClick={() => onOpen(true)}
      >
        + Tab
      </button>
      <button
        className={`btn ghost mv-history-btn mv-pin-btn${job.pinned ? " active" : ""}`}
        title={job.pinned ? "Unpin" : pinDisabled ? `Max ${MAX_PINNED} pinned` : "Pin"}
        disabled={pinDisabled}
        onClick={onPinToggle}
      >
        {busy ? "…" : job.pinned ? "unpin" : "pin"}
      </button>
    </div>
  );
}
