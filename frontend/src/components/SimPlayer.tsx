import { useEffect, useRef, useState, useCallback } from "react";
import { useAuth } from "@clerk/clerk-react";
import { unzipSync } from "fflate";
import { useApi } from "../api/client";
import { SimStore } from "../sim/store";
import { SimRenderer } from "../sim/renderer";
import type { SimMessage } from "../sim/types";
import type { TestMatchJob } from "../api/types";

const BASE_WS_URL = (import.meta.env.VITE_API_BASE_URL ?? "")
  .replace(/\/$/, "")
  .replace(/^http/, "ws");

const SPEEDS = [1, 2, 4, 8];
const MS_PER_STEP_1X = 100;

interface Highlight {
  step: number;
  kind: "death" | "trap";
  snakeIdx: number;
  trappingIdx?: number; // only for trap kind
}

interface Props {
  job: TestMatchJob;
  onConsoleLog?: (log: string | null) => void;
  onJobStatus?: (status: string) => void;   // running / success / failure
  onBuildStatus?: (status: string) => void;  // dev_build_status: building/built/ready/crashed/failed
}

type Status = "connecting" | "live" | "loading" | "ended" | "failed" | "error";

export function SimPlayer({ job, onConsoleLog, onJobStatus, onBuildStatus }: Props) {
  const { getToken } = useAuth();
  const api = useApi();
  const canvasRef    = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const storeRef     = useRef(new SimStore());
  const rendererRef  = useRef<SimRenderer | null>(null);
  // Grid dimensions from the "start" message — null until known.
  const gridSizeRef  = useRef<{ width: number; height: number } | null>(null);

  const currentStepRef = useRef(0);
  const totalStepsRef  = useRef(0);

  const [status, setStatus]           = useState<Status>("connecting");
  const [totalSteps, setTotalSteps]   = useState(0);
  const [currentStep, setCurrentStep] = useState(0);
  const [playing, setPlaying]         = useState(false);
  const [speedIdx, setSpeedIdx]       = useState(0);
  const [errorMsg, setErrorMsg]       = useState("");
  const [highlights, setHighlights]   = useState<Highlight[]>([]);
  const [hoveredMark, setHoveredMark] = useState<Highlight | null>(null);
  const highlightsRef = useRef<Highlight[]>([]);
  highlightsRef.current = highlights;

  currentStepRef.current = currentStep;
  totalStepsRef.current  = totalSteps;

  const speed = SPEEDS[speedIdx];

  // ── Render ────────────────────────────────────────────────────────────────
  const renderStep = useCallback((step: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (!rendererRef.current) rendererRef.current = new SimRenderer(canvas);
    const meta  = storeRef.current.startData;
    const state = storeRef.current.getStateAtStep(step);
    if (meta && state) rendererRef.current.render(state, meta);
  }, []);

  // ── Canvas sizing — fit grid aspect ratio inside the container ────────────
  // All sizing is done in JS so it works regardless of CSS overflow context.
  // The container (flex: 1) always fills the available pane space. We compute
  // the largest rectangle with the grid's aspect ratio that fits inside it,
  // then set both the pixel buffer and inline style dimensions on the canvas.
  const resizeCanvas = useCallback(() => {
    const wrap   = containerRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    const cw = wrap.clientWidth;
    const ch = wrap.clientHeight;
    if (cw <= 0 || ch <= 0) return;

    const gs = gridSizeRef.current;
    let pw: number, ph: number;
    if (gs) {
      const scale = Math.min(cw / gs.width, ch / gs.height);
      pw = Math.round(gs.width  * scale);
      ph = Math.round(gs.height * scale);
    } else {
      pw = Math.round(cw);
      ph = Math.round(ch);
    }
    canvas.width        = pw;
    canvas.height       = ph;
    canvas.style.width  = `${pw}px`;
    canvas.style.height = `${ph}px`;
    renderStep(currentStepRef.current);
  }, [renderStep]);

  // Stable refs so WS closures always call the latest callbacks without
  // being included in connectWs's dependency array (which would restart the WS).
  const resizeCanvasRef = useRef(resizeCanvas);
  resizeCanvasRef.current = resizeCanvas;

  const onConsoleLogRef = useRef(onConsoleLog);
  onConsoleLogRef.current = onConsoleLog;

  const onJobStatusRef = useRef(onJobStatus);
  onJobStatusRef.current = onJobStatus;

  const onBuildStatusRef = useRef(onBuildStatus);
  onBuildStatusRef.current = onBuildStatus;

  // ── Re-size whenever the container changes dimensions ────────────────────
  useEffect(() => {
    const wrap = containerRef.current;
    if (!wrap) return;
    const obs = new ResizeObserver(() => resizeCanvas());
    obs.observe(wrap);
    return () => obs.disconnect();
  }, [resizeCanvas]);

  // ── Render and update console whenever current step changes ──────────────
  useEffect(() => {
    renderStep(currentStep);
    onConsoleLogRef.current?.(storeRef.current.getDevLogs(currentStep));
  }, [currentStep, renderStep]);

  // ── Load bundle from file host (completed matches) ────────────────────────
  const fetchBundleFiles = useCallback(async (jobId: number) => {
    let urlResult: Awaited<ReturnType<typeof api.getTestMatchBundleUrl>>;
    for (let i = 0; ; i++) {
      try {
        urlResult = await api.getTestMatchBundleUrl(jobId);
        break;
      } catch (e) {
        if (i >= 8) throw e;
        await new Promise((r) => setTimeout(r, 750));
      }
    }
    const { url } = urlResult!;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(
      resp.status === 404
        ? "replay recording not found — it may have been deleted from storage"
        : `failed to fetch replay (${resp.status})`,
    );
    return unzipSync(new Uint8Array(await resp.arrayBuffer()));
  }, [api]);

  // Called after a live WS stream ends — only loads highlights without
  // resetting the store or rewinding playback.
  const loadAnalysis = useCallback(async (jobId: number) => {
    try {
      const files = await fetchBundleFiles(jobId);
      const analysisFile = files["analysis.json"];
      if (!analysisFile) return;
      const analysis = JSON.parse(new TextDecoder().decode(analysisFile)) as {
        fatal_steps?: Record<string, number>;
        traps_mapping?: Record<string, Array<{ trapped_ids: number[]; trapping_ids: number[] }>>;
      };
      const newHighlights: Highlight[] = [];
      for (const [snakeId, step] of Object.entries(analysis.fatal_steps ?? {})) {
        newHighlights.push({ step, kind: "death", snakeIdx: Number(snakeId) });
      }
      for (const [stepStr, trapInfos] of Object.entries(analysis.traps_mapping ?? {})) {
        const step = Number(stepStr);
        for (const trap of trapInfos) {
          newHighlights.push({ step, kind: "trap", snakeIdx: trap.trapped_ids[0] ?? 0, trappingIdx: trap.trapping_ids[0] });
        }
      }
      setHighlights(newHighlights);
    } catch {
      // highlights are non-critical
    }
  }, [fetchBundleFiles]);

  const loadAnalysisRef = useRef(loadAnalysis);
  loadAnalysisRef.current = loadAnalysis;

  const loadBundle = useCallback(async (jobId: number) => {
    setStatus("loading");
    try {
      const files = await fetchBundleFiles(jobId);
      const replayText = new TextDecoder().decode(files["replay.json"]);
      // replay.json is JSONL — one {type,data} message per line.
      const messages: SimMessage[] = replayText
        .split("\n")
        .filter((line) => line.trim() !== "")
        .map((line) => JSON.parse(line) as SimMessage);

      storeRef.current.reset();
      for (const msg of messages) {
        storeRef.current.addMessage(msg);
        if (msg.type === "start") {
          const d = msg.data.env_meta_data;
          gridSizeRef.current = { width: d.width, height: d.height };
        }
      }
      // Load agent logs from the bundle if present (separate file to keep
      // replay.json lean — the logs aren't needed for playback itself).
      const agentLogsFile = files["agent_logs.json"];
      if (agentLogsFile) {
        const agentLogs = JSON.parse(new TextDecoder().decode(agentLogsFile)) as Record<string, string[]>;
        (agentLogs["0"] ?? []).forEach((log, step) => {
          storeRef.current.addMessage({ type: "step_log", data: { step, log } });
        });
      }
      const analysisFile = files["analysis.json"];
      if (analysisFile) {
        try {
          const analysis = JSON.parse(new TextDecoder().decode(analysisFile)) as {
            fatal_steps?: Record<string, number>;
            traps_mapping?: Record<string, Array<{ trapped_ids: number[]; trapping_ids: number[] }>>;
          };
          const newHighlights: Highlight[] = [];
          for (const [snakeId, step] of Object.entries(analysis.fatal_steps ?? {})) {
            newHighlights.push({ step, kind: "death", snakeIdx: Number(snakeId) });
          }
          for (const [stepStr, trapInfos] of Object.entries(analysis.traps_mapping ?? {})) {
            const step = Number(stepStr);
            for (const trap of trapInfos) {
              const snakeIdx = trap.trapped_ids[0] ?? 0;
              const trappingIdx = trap.trapping_ids[0];
              newHighlights.push({ step, kind: "trap", snakeIdx, trappingIdx });
            }
          }
          setHighlights(newHighlights);
        } catch (err) {
          console.error("analysis.json parse error", err);
        }
      }

      setTotalSteps(storeRef.current.frameCount);
      setCurrentStep(0);
      setStatus("ended");
      setPlaying(true);
      resizeCanvas(); // sizes canvas correctly + renders frame 0
      onConsoleLogRef.current?.(storeRef.current.getDevLogs(0));
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : "failed to load bundle");
      setStatus("error");
    }
  }, [fetchBundleFiles, resizeCanvas]);

  // Stable ref so the WS handler can trigger a bundle load (on a snapshot that
  // shows the match already finished) without depending on loadBundle.
  const loadBundleRef = useRef(loadBundle);
  loadBundleRef.current = loadBundle;

  // ── WebSocket for live matches ────────────────────────────────────────────
  const connectWs = useCallback((jobId: number) => {
    storeRef.current.reset();
    setStatus("connecting");
    setTotalSteps(0);
    setCurrentStep(0);
    setPlaying(false);

    let ws: WebSocket | null = null;
    let cancelled = false;

    getToken().then((token) => {
      if (cancelled || !token) return;
      ws = new WebSocket(
        `${BASE_WS_URL}/test-matches/${jobId}/ws?token=${encodeURIComponent(token)}`,
      );

      ws.onmessage = (evt) => {
        const msg = JSON.parse(evt.data as string) as SimMessage;
        storeRef.current.addMessage(msg);

        if (msg.type === "snapshot") {
          // Current state on connect. Reflect build status; if already done,
          // the live stream is over — fetch the bundle instead.
          if (msg.data.build_status) onBuildStatusRef.current?.(msg.data.build_status);
          if (["success", "failure", "cancelled"].includes(msg.data.job_status)) {
            ws?.close();
            if (msg.data.job_status === "failure") {
              setStatus("failed");
              setErrorMsg(msg.data.error ?? "match failed");
            } else {
              void loadBundleRef.current(jobId);
            }
          }
        } else if (msg.type === "build") {
          // data.status is the dev_build_status value directly.
          onBuildStatusRef.current?.(msg.data.status);
          if (msg.data.status === "failed") {
            setStatus("failed");
            const err = msg.data.error ?? "build failed";
            setErrorMsg(err);
            onConsoleLogRef.current?.(err);
          }
        } else if (msg.type === "status") {
          onJobStatusRef.current?.(msg.data.status);
          if (msg.data.status === "success") {
            void loadAnalysisRef.current(jobId);
          }
        } else if (msg.type === "start") {
          const d = msg.data.env_meta_data;
          gridSizeRef.current = { width: d.width, height: d.height };
          resizeCanvasRef.current();
          setStatus("live");
          setCurrentStep(0);
          setTotalSteps(storeRef.current.frameCount); // frame 0 = start state
          setPlaying(true);
        } else if (msg.type === "step") {
          setPlaying(true);
          setTotalSteps(storeRef.current.frameCount);
        } else if (msg.type === "step_log") {
          // Step log arrived — refresh the console for the current step.
          onConsoleLogRef.current?.(storeRef.current.getDevLogs(currentStepRef.current));
        } else if (msg.type === "stop") {
          setStatus("ended");
        } else if (msg.type === "error") {
          setStatus("failed");
          setErrorMsg(msg.data.message);
        }
      };

      ws.onerror = () => setStatus("error");
      ws.onclose = () => {
        if (!cancelled && status === "live") setStatus("ended");
      };
    });

    return () => {
      cancelled = true;
      ws?.close();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [getToken]);

  // ── Route: live WS vs file fetch ──────────────────────────────────────────
  useEffect(() => {
    storeRef.current.reset();
    gridSizeRef.current = null;
    setTotalSteps(0);
    setCurrentStep(0);
    setPlaying(false);
    setErrorMsg("");
    setHighlights([]);
    onConsoleLogRef.current?.(null);

    if (job.status === "failure") {
      setErrorMsg(job.error ?? "match failed");
      setStatus("failed");
      return;
    }

    if (job.status === "queued" || job.status === "running") {
      return connectWs(job.id);
    }

    void loadBundle(job.id);
  // Only re-run when the job itself changes, not on status updates —
  // a live WS stream that received "stop" already has all the data it needs.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id]);

  // ── Playback interval ─────────────────────────────────────────────────────
  useEffect(() => {
    if (!playing) return;
    let id: number;
    const tick = () => {
      const total = totalStepsRef.current;
      const current = currentStepRef.current;
      if (total === 0 || current >= total - 1) {
        // No new step yet — poll quickly rather than stopping.
        id = window.setTimeout(tick, 5);
      } else {
        setCurrentStep(current + 1);
        id = window.setTimeout(tick, MS_PER_STEP_1X / speed);
      }
    };
    id = window.setTimeout(tick, MS_PER_STEP_1X / speed);
    return () => window.clearTimeout(id);
  }, [playing, speed]);

  // ── Controls ──────────────────────────────────────────────────────────────
  const handleScrub = (e: React.ChangeEvent<HTMLInputElement>) => {
    setCurrentStep(Number(e.target.value));
    setPlaying(false);
  };

  const handleTimelineClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (totalStepsRef.current <= 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const step = Math.round(ratio * (totalStepsRef.current - 1));
    setCurrentStep(step);
    setPlaying(false);
  }, []);

  const handleTimelineMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const total = totalStepsRef.current;
    const hl = highlightsRef.current;
    if (total <= 1 || hl.length === 0) { setHoveredMark(null); return; }
    const rect = e.currentTarget.getBoundingClientRect();
    const cursorStep = ((e.clientX - rect.left) / rect.width) * (total - 1);
    // Snap to nearest mark within 3% of total steps (min 2 steps).
    const threshold = Math.max(2, total * 0.03);
    let best: Highlight | null = null;
    let bestDist = Infinity;
    for (const h of hl) {
      const d = Math.abs(h.step - cursorStep);
      if (d < bestDist) { bestDist = d; best = h; }
    }
    setHoveredMark(bestDist <= threshold ? best : null);
  }, []);

  const togglePlay = () => {
    if (currentStep >= totalSteps - 1 && !playing) setCurrentStep(0);
    setPlaying((p) => !p);
  };

  const cycleSpeed = () => setSpeedIdx((i) => (i + 1) % SPEEDS.length);

  const showControls = totalSteps > 0;
  const snakeLegend  = storeRef.current.startData?.snake_tags;
  const SNAKE_COLORS = ["#b8ff3c","#60a5fa","#f87171","#fb923c","#a78bfa","#34d399"];

  const agentName = (idx: number) => {
    const name = job.participant_names[idx] ?? `snake ${idx}`;
    return idx === 0 ? `${name} (dev)` : name;
  };
  const markLabel = (h: Highlight) => {
    if (h.kind === "death") return `${agentName(h.snakeIdx)} died · step ${h.step + 1}`;
    if (h.trappingIdx !== undefined)
      return `${agentName(h.snakeIdx)} trapped by ${agentName(h.trappingIdx)} · step ${h.step + 1}`;
    return `${agentName(h.snakeIdx)} trapped · step ${h.step + 1}`;
  };

  return (
    <div className="sim-player">
      <div className="sim-canvas-wrap" ref={containerRef}>
        <canvas ref={canvasRef} className="sim-canvas" />
        {(status === "connecting" || status === "loading") && (
          <div className="sim-overlay">
            {status === "loading" ? "loading replay…" : "connecting…"}
          </div>
        )}
        {status === "failed" && (
          <div className="sim-overlay sim-overlay-err">{errorMsg || "match failed"}</div>
        )}
        {status === "error" && (
          <div className="sim-overlay sim-overlay-err">{errorMsg || "connection error"}</div>
        )}
        {status === "live" && currentStep >= totalSteps - 1 && totalSteps > 0 && (
          <div className="sim-live-badge">● LIVE</div>
        )}
      </div>

      {snakeLegend && (
        <div className="sim-legend">
          {Object.entries(snakeLegend).map(([id]) => {
            const seat = Number(id);
            const baseName = job.participant_names[seat] ?? id;
            const label = seat === 0 ? `${baseName} (dev)` : baseName;
            return (
              <span key={id} className="sim-legend-item">
                <span
                  className="sim-legend-dot"
                  style={{ background: SNAKE_COLORS[seat % SNAKE_COLORS.length] }}
                />
                {label}
              </span>
            );
          })}
        </div>
      )}

      {showControls && (
        <div className="sim-controls">
          <button className="btn ghost sim-ctrl-btn" onClick={togglePlay}>
            {playing ? "⏸" : "▶"}
          </button>
          <div className="sim-scrubber-col">
            <input
              type="range"
              className="sim-scrubber"
              min={0}
              max={Math.max(0, totalSteps - 1)}
              value={currentStep}
              onChange={handleScrub}
            />
            <div
              className="sim-timeline"
              aria-label="timeline"
              onClick={totalSteps > 0 ? handleTimelineClick : undefined}
              onMouseMove={totalSteps > 1 ? handleTimelineMouseMove : undefined}
              onMouseLeave={() => setHoveredMark(null)}
              style={totalSteps > 0 ? { cursor: "pointer" } : undefined}
            >
              {totalSteps > 1 && highlights.map((h, i) => (
                <div
                  key={i}
                  className={`sim-mark sim-mark-${h.kind}`}
                  style={{ left: `${(h.step / (totalSteps - 1)) * 100}%` }}
                />
              ))}
              {hoveredMark && totalSteps > 1 && (
                <div
                  className={`sim-mark-tooltip sim-mark-tooltip-${hoveredMark.kind}`}
                  style={{ left: `${(hoveredMark.step / (totalSteps - 1)) * 100}%` }}
                >
                  {markLabel(hoveredMark)}
                </div>
              )}
            </div>
          </div>
          <span className="sim-step-counter">{currentStep + 1} / {totalSteps}</span>
          <button className="btn ghost sim-ctrl-btn" onClick={cycleSpeed} title="playback speed">
            {speed}×
          </button>
        </div>
      )}
    </div>
  );
}
