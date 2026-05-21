import { useEffect, useRef, useState, useCallback } from "react";
import { useAuth } from "@clerk/clerk-react";
import { unzipSync } from "fflate";
import { useApi } from "../api/client";
import { SimStore } from "../sim/store";
import { SimRenderer } from "../sim/renderer";
import type { SimMessage, SimStartData } from "../sim/types";
import type { TestMatchJob } from "../api/types";

const BASE_WS_URL = (import.meta.env.VITE_API_BASE_URL ?? "")
  .replace(/\/$/, "")
  .replace(/^http/, "ws");

const SPEEDS = [1, 2, 4, 8];
const MS_PER_STEP_1X = 100;

interface Props {
  job: TestMatchJob;
}

type Status = "connecting" | "live" | "loading" | "ended" | "failed" | "error";

export function SimPlayer({ job }: Props) {
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

  // Stable ref so WS closures always call the latest resizeCanvas without
  // being included in connectWs's dependency array (which would restart the WS).
  const resizeCanvasRef = useRef(resizeCanvas);
  resizeCanvasRef.current = resizeCanvas;

  // ── Re-size whenever the container changes dimensions ────────────────────
  useEffect(() => {
    const wrap = containerRef.current;
    if (!wrap) return;
    const obs = new ResizeObserver(() => resizeCanvas());
    obs.observe(wrap);
    return () => obs.disconnect();
  }, [resizeCanvas]);

  // ── Render whenever current step changes ─────────────────────────────────
  useEffect(() => {
    renderStep(currentStep);
  }, [currentStep, renderStep]);

  // ── Load bundle from file host (completed matches) ────────────────────────
  const loadBundle = useCallback(async (jobId: number) => {
    setStatus("loading");
    try {
      const { url } = await api.getTestMatchBundleUrl(jobId);
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(
        resp.status === 404
          ? "replay recording not found — it may have been deleted from storage"
          : `failed to fetch replay (${resp.status})`,
      );
      const buf   = await resp.arrayBuffer();
      const files = unzipSync(new Uint8Array(buf));
      const replayText = new TextDecoder().decode(files["replay.json"]);
      const messages: SimMessage[] = JSON.parse(replayText);

      storeRef.current.reset();
      for (const msg of messages) {
        storeRef.current.addMessage(msg);
        if (msg.type === "start") {
          const d = msg.data as SimStartData;
          gridSizeRef.current = { width: d.width, height: d.height };
        }
      }
      const n = storeRef.current.stepCount;
      setTotalSteps(n);
      setCurrentStep(0);
      setStatus("ended");
      setPlaying(true);
      resizeCanvas(); // sizes canvas correctly + renders frame 0
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : "failed to load bundle");
      setStatus("error");
    }
  }, [api, resizeCanvas]);

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

        if (msg.type === "start") {
          const d = msg.data as SimStartData;
          gridSizeRef.current = { width: d.width, height: d.height };
          resizeCanvasRef.current();
          setStatus("live");
          setCurrentStep(0);
          setPlaying(true);
        } else if (msg.type === "step") {
          const n = storeRef.current.stepCount;
          setPlaying(true);
          setTotalSteps(n);
        } else if (msg.type === "stop") {
          setStatus("ended");
          ws?.close();
        } else if (msg.type === "error") {
          setStatus("failed");
          setErrorMsg((msg as { type: "error"; data: { message: string } }).data.message);
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

  const togglePlay = () => {
    if (currentStep >= totalSteps - 1 && !playing) setCurrentStep(0);
    setPlaying((p) => !p);
  };

  const cycleSpeed = () => setSpeedIdx((i) => (i + 1) % SPEEDS.length);

  const showControls = totalSteps > 0;
  const snakeLegend  = storeRef.current.startData?.snake_tags;
  const SNAKE_COLORS = ["#b8ff3c","#60a5fa","#f87171","#fb923c","#a78bfa","#34d399"];

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
          {Object.entries(snakeLegend).map(([id, name]) => (
            <span key={id} className="sim-legend-item">
              <span
                className="sim-legend-dot"
                style={{ background: SNAKE_COLORS[Number(id) % SNAKE_COLORS.length] }}
              />
              {name}
            </span>
          ))}
        </div>
      )}

      {showControls && (
        <div className="sim-controls">
          <button className="btn ghost sim-ctrl-btn" onClick={togglePlay}>
            {playing ? "⏸" : "▶"}
          </button>
          <input
            type="range"
            className="sim-scrubber"
            min={0}
            max={Math.max(0, totalSteps - 1)}
            value={currentStep}
            onChange={handleScrub}
          />
          <span className="sim-step-counter">{currentStep + 1} / {totalSteps}</span>
          <button className="btn ghost sim-ctrl-btn" onClick={cycleSpeed} title="playback speed">
            {speed}×
          </button>
        </div>
      )}

      <div className="sim-timeline" aria-label="timeline" />
    </div>
  );
}
