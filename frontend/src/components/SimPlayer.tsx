import { useEffect, useRef, useState, useCallback } from "react";
import { useAuth } from "@clerk/clerk-react";
import { SimStore } from "../sim/store";
import { SimRenderer } from "../sim/renderer";
import type { SimMessage } from "../sim/types";

const BASE_WS_URL = (import.meta.env.VITE_API_BASE_URL ?? "")
  .replace(/\/$/, "")
  .replace(/^http/, "ws");

const SPEEDS = [1, 2, 4, 8];
const MS_PER_STEP_1X = 100; // 10 steps/sec at 1×

interface Props {
  jobId: number;
}

type Status = "connecting" | "live" | "ended" | "failed" | "error";

export function SimPlayer({ jobId }: Props) {
  const { getToken } = useAuth();
  const canvasRef    = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const storeRef     = useRef(new SimStore());
  const rendererRef  = useRef<SimRenderer | null>(null);

  // Mutable ref so the ResizeObserver closure always sees the latest step
  // without needing to be recreated every time currentStep changes.
  const currentStepRef = useRef(0);

  const [status, setStatus]           = useState<Status>("connecting");
  const [totalSteps, setTotalSteps]   = useState(0);
  const [currentStep, setCurrentStep] = useState(0);
  const [playing, setPlaying]         = useState(false);
  const [speedIdx, setSpeedIdx]       = useState(0);
  const [errorMsg, setErrorMsg]       = useState("");
  // CSS aspect-ratio string e.g. "20 / 20" — set once we know the grid size.
  const [gridAspect, setGridAspect]   = useState<string | null>(null);

  currentStepRef.current = currentStep;

  const speed  = SPEEDS[speedIdx];
  const isLive = status === "live";

  // ── Stable render helper ──────────────────────────────────────────────────
  const renderStep = useCallback((step: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (!rendererRef.current) rendererRef.current = new SimRenderer(canvas);
    const meta  = storeRef.current.startData;
    const state = storeRef.current.getStateAtStep(step);
    if (meta && state) rendererRef.current.render(state, meta);
  }, []);

  // ── Connect to WebSocket ──────────────────────────────────────────────────
  useEffect(() => {
    storeRef.current.reset();
    setStatus("connecting");
    setTotalSteps(0);
    setCurrentStep(0);
    setPlaying(false);
    setGridAspect(null);

    let ws: WebSocket | null = null;
    let cancelled = false;

    getToken().then((token) => {
      if (cancelled || !token) return;
      ws = new WebSocket(`${BASE_WS_URL}/test-matches/${jobId}/ws?token=${encodeURIComponent(token)}`);

      ws.onmessage = (evt) => {
        const msg = JSON.parse(evt.data as string) as SimMessage;
        storeRef.current.addMessage(msg);

        if (msg.type === "start") {
          setGridAspect(`${msg.data.width} / ${msg.data.height}`);
          setStatus("live");
          setCurrentStep(0);
        } else if (msg.type === "step") {
          const n = storeRef.current.stepCount;
          setTotalSteps(n);
          setCurrentStep(n - 1); // auto-follow while live
        } else if (msg.type === "stop") {
          setStatus("ended");
          setPlaying(false);
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
  }, [jobId]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Sync canvas pixel buffer to its CSS-rendered size ────────────────────
  // We observe the canvas element itself so the ResizeObserver fires whenever
  // the CSS size changes — which happens both on container resize AND when the
  // aspect-ratio style is applied/changed.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const obs = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) {
        canvas.width  = Math.round(width);
        canvas.height = Math.round(height);
        renderStep(currentStepRef.current);
      }
    });
    obs.observe(canvas);
    return () => obs.disconnect();
  }, [renderStep]);

  // ── Render whenever current step changes ─────────────────────────────────
  useEffect(() => {
    renderStep(currentStep);
  }, [currentStep, renderStep]);

  // ── Replay playback interval ──────────────────────────────────────────────
  useEffect(() => {
    if (isLive || !playing || totalSteps === 0) return;
    const id = setInterval(() => {
      setCurrentStep((prev) => {
        const next = prev + 1;
        if (next >= totalSteps) { setPlaying(false); return totalSteps - 1; }
        return next;
      });
    }, MS_PER_STEP_1X / speed);
    return () => clearInterval(id);
  }, [isLive, playing, speed, totalSteps]);

  // ── Controls ──────────────────────────────────────────────────────────────
  const handleScrub = (e: React.ChangeEvent<HTMLInputElement>) => {
    setCurrentStep(Number(e.target.value));
    if (!isLive) setPlaying(false);
  };

  const togglePlay = () => {
    if (isLive) return;
    if (currentStep >= totalSteps - 1 && !playing) setCurrentStep(0);
    setPlaying((p) => !p);
  };

  const cycleSpeed = () => setSpeedIdx((i) => (i + 1) % SPEEDS.length);

  const showControls = status === "ended" || (status === "live" && totalSteps > 0);
  const snakeLegend  = storeRef.current.startData?.snake_tags;
  const SNAKE_COLORS = ["#b8ff3c","#60a5fa","#f87171","#fb923c","#a78bfa","#34d399"];

  return (
    <div className="sim-player">
      {/* Canvas — aspect-ratio is set once we know the grid dimensions.
          The container is a flex centering box; the canvas shrinks to fit. */}
      <div className="sim-canvas-wrap" ref={containerRef}>
        <canvas
          ref={canvasRef}
          className="sim-canvas"
          style={gridAspect
            ? { aspectRatio: gridAspect, maxWidth: "100%", maxHeight: "100%" }
            : { width: "100%", height: "100%" }
          }
        />
        {status === "connecting" && <div className="sim-overlay">connecting…</div>}
        {status === "failed"     && <div className="sim-overlay sim-overlay-err">{errorMsg || "match failed"}</div>}
        {status === "error"      && <div className="sim-overlay sim-overlay-err">connection error</div>}
        {status === "live"       && <div className="sim-live-badge">● LIVE</div>}
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
          {!isLive && (
            <button className="btn ghost sim-ctrl-btn" onClick={togglePlay}>
              {playing ? "⏸" : "▶"}
            </button>
          )}
          <input
            type="range"
            className="sim-scrubber"
            min={0}
            max={Math.max(0, totalSteps - 1)}
            value={currentStep}
            onChange={handleScrub}
            disabled={isLive}
          />
          <span className="sim-step-counter">{currentStep + 1} / {totalSteps}</span>
          {!isLive && (
            <button className="btn ghost sim-ctrl-btn" onClick={cycleSpeed} title="playback speed">
              {speed}×
            </button>
          )}
        </div>
      )}

      <div className="sim-timeline" aria-label="timeline" />
    </div>
  );
}
