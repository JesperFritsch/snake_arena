import { useEffect, useRef, useState, useCallback } from "react";
import { unzipSync } from "fflate";
import { SimStore } from "../sim/store";
import { SimRenderer } from "../sim/renderer";
import type { SimMessage } from "../sim/types";
import type { RankedMatchSummary } from "../api/types";

const SPEEDS = [1, 2, 4, 8];
const MS_PER_STEP_1X = 100;
const SNAKE_COLORS = ["#b8ff3c", "#60a5fa", "#f87171", "#fb923c", "#a78bfa", "#34d399"];

interface Highlight {
  step: number;
  kind: "death" | "trap";
  snakeIdx: number;
  trappingIdx?: number;
}

function parseHighlights(data: Uint8Array): Highlight[] {
  const analysis = JSON.parse(new TextDecoder().decode(data)) as {
    fatal_steps?: Record<string, number>;
    traps_mapping?: Record<string, Array<{ trapped_ids: number[]; trapping_ids: number[] }>>;
  };
  const out: Highlight[] = [];
  for (const [id, step] of Object.entries(analysis.fatal_steps ?? {})) {
    out.push({ step, kind: "death", snakeIdx: Number(id) });
  }
  for (const [stepStr, infos] of Object.entries(analysis.traps_mapping ?? {})) {
    const step = Number(stepStr);
    for (const t of infos) {
      out.push({ step, kind: "trap", snakeIdx: t.trapped_ids[0] ?? 0, trappingIdx: t.trapping_ids[0] });
    }
  }
  return out;
}

interface Props {
  match: RankedMatchSummary;
  getBundleUrl: () => Promise<{ url: string }>;
}

type Status = "loading" | "ready" | "error";

export function RankedMatchPlayer({ match, getBundleUrl }: Props) {
  const canvasRef    = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const storeRef     = useRef(new SimStore());
  const rendererRef  = useRef<SimRenderer | null>(null);
  const gridSizeRef  = useRef<{ width: number; height: number } | null>(null);
  const currentStepRef = useRef(0);
  const totalStepsRef  = useRef(0);

  const [status, setStatus]           = useState<Status>("loading");
  const [errorMsg, setErrorMsg]       = useState("");
  const [totalSteps, setTotalSteps]   = useState(0);
  const [currentStep, setCurrentStep] = useState(0);
  const [playing, setPlaying]         = useState(false);
  const [speedIdx, setSpeedIdx]       = useState(0);
  const [highlights, setHighlights]   = useState<Highlight[]>([]);
  const [hoveredMark, setHoveredMark] = useState<Highlight | null>(null);
  const [execTimes, setExecTimes]     = useState<Record<string, number> | null>(null);

  const highlightsRef = useRef<Highlight[]>([]);
  highlightsRef.current   = highlights;
  currentStepRef.current  = currentStep;
  totalStepsRef.current   = totalSteps;

  const speed = SPEEDS[speedIdx];

  // Participant names ordered by seat.
  const participantNames = match.participants
    .slice()
    .sort((a, b) => a.seat - b.seat)
    .map((p) => p.project_name);

  // ── Render ───────────────────────────────────────────────────────────────
  const renderStep = useCallback((step: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (!rendererRef.current) rendererRef.current = new SimRenderer(canvas);
    const meta  = storeRef.current.startData;
    const state = storeRef.current.getStateAtStep(step);
    if (meta && state) rendererRef.current.render(state, meta);
  }, []);

  // ── Canvas sizing ────────────────────────────────────────────────────────
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

  const resizeCanvasRef = useRef(resizeCanvas);
  resizeCanvasRef.current = resizeCanvas;

  useEffect(() => {
    const wrap = containerRef.current;
    if (!wrap) return;
    const obs = new ResizeObserver(() => resizeCanvas());
    obs.observe(wrap);
    return () => obs.disconnect();
  }, [resizeCanvas]);

  // Update render + exec times whenever step changes.
  useEffect(() => {
    renderStep(currentStep);
    const et = storeRef.current.getExecTimes(currentStep);
    setExecTimes(et && Object.keys(et).length > 0 ? et : null);
  }, [currentStep, renderStep]);

  // ── Load bundle on mount (parent uses key={match.id} to remount) ─────────
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { url } = await getBundleUrl();
        if (cancelled) return;
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`Failed to fetch replay (${resp.status})`);
        const files = unzipSync(new Uint8Array(await resp.arrayBuffer()));
        if (cancelled) return;

        const replayText = new TextDecoder().decode(files["replay.json"]);
        const messages: SimMessage[] = replayText
          .split("\n")
          .filter((l) => l.trim() !== "")
          .map((l) => JSON.parse(l) as SimMessage);

        for (const msg of messages) {
          storeRef.current.addMessage(msg);
          if (msg.type === "start") {
            const d = msg.data.env_meta_data;
            gridSizeRef.current = { width: d.width, height: d.height };
          }
        }

        const execTimesFile = files["exec_times.json"];
        if (execTimesFile) {
          const etData = JSON.parse(new TextDecoder().decode(execTimesFile)) as Record<string, number[]>;
          const stepCount = Math.max(0, ...Object.values(etData).map((a) => a.length));
          for (let step = 0; step < stepCount; step++) {
            const times: Record<string, number> = {};
            for (const [snakeId, arr] of Object.entries(etData)) {
              if (arr[step] !== undefined) times[snakeId] = arr[step];
            }
            storeRef.current.addMessage({ type: "exec_time", data: { step, times } });
          }
        }

        const analysisFile = files["analysis.json"];
        if (analysisFile) {
          try { setHighlights(parseHighlights(analysisFile)); } catch { /* non-critical */ }
        }

        if (cancelled) return;
        setTotalSteps(storeRef.current.frameCount);
        setCurrentStep(0);
        setStatus("ready");
        setPlaying(true);
        resizeCanvasRef.current();
        const et0 = storeRef.current.getExecTimes(0);
        setExecTimes(et0 && Object.keys(et0).length > 0 ? et0 : null);
      } catch (e) {
        if (!cancelled) {
          setErrorMsg(e instanceof Error ? e.message : "Failed to load replay");
          setStatus("error");
        }
      }
    })();
    return () => { cancelled = true; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Playback interval ────────────────────────────────────────────────────
  useEffect(() => {
    if (!playing) return;
    let id: number;
    const tick = () => {
      const total   = totalStepsRef.current;
      const current = currentStepRef.current;
      if (total === 0 || current >= total - 1) {
        id = window.setTimeout(tick, 5);
      } else {
        setCurrentStep(current + 1);
        id = window.setTimeout(tick, MS_PER_STEP_1X / speed);
      }
    };
    id = window.setTimeout(tick, MS_PER_STEP_1X / speed);
    return () => window.clearTimeout(id);
  }, [playing, speed]);

  // ── Controls ─────────────────────────────────────────────────────────────
  const handleScrub = (e: React.ChangeEvent<HTMLInputElement>) => {
    setCurrentStep(Number(e.target.value));
    setPlaying(false);
  };

  const handleTimelineClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (totalStepsRef.current <= 0) return;
    const rect  = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    setCurrentStep(Math.round(ratio * (totalStepsRef.current - 1)));
    setPlaying(false);
  }, []);

  const handleTimelineMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const total = totalStepsRef.current;
    const hl    = highlightsRef.current;
    if (total <= 1 || hl.length === 0) { setHoveredMark(null); return; }
    const rect       = e.currentTarget.getBoundingClientRect();
    const cursorStep = ((e.clientX - rect.left) / rect.width) * (total - 1);
    const threshold  = Math.max(2, total * 0.03);
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

  const snakeLegend = storeRef.current.startData?.snake_tags;
  const showControls = totalSteps > 0;

  const markLabel = (h: Highlight) => {
    const name = participantNames[h.snakeIdx] ?? `snake ${h.snakeIdx}`;
    if (h.kind === "death") return `${name} died · step ${h.step + 1}`;
    const trapper = participantNames[h.trappingIdx ?? -1] ?? `snake ${h.trappingIdx}`;
    return `${name} trapped by ${trapper} · step ${h.step + 1}`;
  };

  return (
    <div className="sim-player">
      <div className="sim-canvas-wrap" ref={containerRef}>
        <canvas ref={canvasRef} className="sim-canvas" />
        {status === "loading" && (
          <div className="sim-overlay">loading replay…</div>
        )}
        {status === "error" && (
          <div className="sim-overlay sim-overlay-err">{errorMsg || "failed to load replay"}</div>
        )}
      </div>

      {snakeLegend && (
        <div className="sim-legend">
          {Object.entries(snakeLegend).map(([id]) => {
            const seat = Number(id);
            return (
              <span key={id} className="sim-legend-item">
                <span
                  className="sim-legend-dot"
                  style={{ background: SNAKE_COLORS[seat % SNAKE_COLORS.length] }}
                />
                {participantNames[seat] ?? id}
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
          <button
            className="btn ghost sim-ctrl-btn"
            onClick={() => setSpeedIdx((i) => (i + 1) % SPEEDS.length)}
            title="playback speed"
          >
            {speed}×
          </button>
        </div>
      )}

      {execTimes && (
        <div className="exec-times-bar">
          <span className="exec-times-label">CPU / step</span>
          {Object.entries(execTimes).map(([id, ms]) => {
            const seat = Number(id);
            return (
              <span key={id} className="exec-times-entry">
                <span
                  className="exec-times-dot"
                  style={{ background: SNAKE_COLORS[seat % SNAKE_COLORS.length] }}
                />
                <span className="exec-times-name">{participantNames[seat] ?? `snake ${seat}`}</span>
                <span className="exec-times-ms">{ms.toFixed(1)}ms</span>
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}
