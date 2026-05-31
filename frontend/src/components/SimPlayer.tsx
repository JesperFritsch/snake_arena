import { useEffect, useRef, useState, useCallback } from "react";
import { SimRenderer } from "../sim/renderer";
import { colorForSeat } from "../sim/colors";
import type { SimStore } from "../sim/store";
import type { SimSource } from "../sim/source";
import type { Highlight } from "../sim/highlights";

const SPEEDS = [1, 2, 4, 8];
const MS_PER_STEP_1X = 100;
// Long-press behavior on prev/next buttons: first step fires on press,
// then after this delay we begin auto-repeating at the interval below.
const HOLD_DELAY_MS = 350;
const HOLD_INTERVAL_MS = 50;

interface Props {
  source: SimSource;
  /** Display names indexed by seat (matches SimRenderer's coloring). */
  participantNames: string[];
  /** Optional seat label decorator (e.g. add " (dev)" to seat 0). */
  labelForSeat?: (seat: number, name: string) => string;
  /** Show a "● LIVE" badge while live & playback is at the latest frame. */
  showLiveBadge?: boolean;
  /** Render the per-step CPU times row inside the player footer. */
  showExecTimesBar?: boolean;
  /** Fires when the rendered step changes or when new step-keyed data
   *  (step_log / exec_time) arrives for the current step. Wrappers use this
   *  to forward dev logs / exec times to a sibling console pane. */
  onStepChange?: (step: number, store: SimStore) => void;
}

export function SimPlayer({
  source,
  participantNames,
  labelForSeat,
  showLiveBadge = false,
  showExecTimesBar = false,
  onStepChange,
}: Props) {
  const { store, status, errorMsg, totalSteps, highlights, gridSize, liveTick } = source;

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const rendererRef = useRef<SimRenderer | null>(null);

  const currentStepRef = useRef(0);
  const totalStepsRef = useRef(0);
  const highlightsRef = useRef<Highlight[]>([]);

  const [currentStep, setCurrentStep] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speedIdx, setSpeedIdx] = useState(0);
  const [hoveredMark, setHoveredMark] = useState<Highlight | null>(null);

  currentStepRef.current = currentStep;
  totalStepsRef.current = totalSteps;
  highlightsRef.current = highlights;

  const speed = SPEEDS[speedIdx];

  // ── Render ───────────────────────────────────────────────────────────────
  const renderStep = useCallback((step: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (!rendererRef.current) rendererRef.current = new SimRenderer(canvas);
    const meta = store.startData;
    const state = store.getStateAtStep(step);
    if (meta && state) rendererRef.current.render(state, meta, store.seatBySnakeId);
  }, [store]);

  // ── Canvas sizing — fit grid aspect ratio inside the container ───────────
  const resizeCanvas = useCallback(() => {
    const wrap = containerRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    const cw = wrap.clientWidth;
    const ch = wrap.clientHeight;
    if (cw <= 0 || ch <= 0) return;
    let pw: number, ph: number;
    if (gridSize) {
      const scale = Math.min(cw / gridSize.width, ch / gridSize.height);
      pw = Math.round(gridSize.width * scale);
      ph = Math.round(gridSize.height * scale);
    } else {
      pw = Math.round(cw);
      ph = Math.round(ch);
    }
    canvas.width = pw;
    canvas.height = ph;
    canvas.style.width = `${pw}px`;
    canvas.style.height = `${ph}px`;
    renderStep(currentStepRef.current);
  }, [renderStep, gridSize]);

  useEffect(() => {
    const wrap = containerRef.current;
    if (!wrap) return;
    const obs = new ResizeObserver(() => resizeCanvas());
    obs.observe(wrap);
    return () => obs.disconnect();
  }, [resizeCanvas]);

  // Re-size whenever the grid dimensions become known (or change).
  useEffect(() => { resizeCanvas(); }, [gridSize, resizeCanvas]);

  // Re-render whenever the displayed step changes.
  useEffect(() => { renderStep(currentStep); }, [currentStep, renderStep]);

  // ── Notify parent of step changes / live data arrival ────────────────────
  const onStepChangeRef = useRef(onStepChange);
  onStepChangeRef.current = onStepChange;
  useEffect(() => {
    onStepChangeRef.current?.(currentStep, store);
  }, [currentStep, liveTick, store]);

  // ── Autoplay on first data: totalSteps transitions from 0 → positive. ────
  // Catches both bundle-loaded ("loading" → "ended") and live ("connecting"
  // → "live" with frame 0). Mid-stream growth (e.g. 5 → 6 as steps stream
  // in) doesn't re-trigger because prevTotalRef is already positive.
  const prevTotalRef = useRef(0);
  useEffect(() => {
    if (prevTotalRef.current === 0 && totalSteps > 0) {
      setCurrentStep(0);
      setPlaying(true);
    }
    prevTotalRef.current = totalSteps;
  }, [totalSteps]);

  // ── Playback loop ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!playing) return;
    let id: number;
    const tick = () => {
      const total = totalStepsRef.current;
      const current = currentStepRef.current;
      if (total === 0 || current >= total - 1) {
        // No new step yet — poll quickly so we resume the moment one arrives.
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
  const stepBy = useCallback((delta: number) => {
    setPlaying(false);
    setCurrentStep((s) => {
      const total = totalStepsRef.current;
      if (total <= 0) return s;
      return Math.max(0, Math.min(total - 1, s + delta));
    });
  }, []);

  const togglePlay = useCallback(() => {
    const total = totalStepsRef.current;
    if (total <= 0) return;
    setPlaying((p) => {
      if (!p && currentStepRef.current >= total - 1) setCurrentStep(0);
      return !p;
    });
  }, []);

  // Long-press auto-repeat for the prev/next buttons. Pointer-based so it
  // works for both mouse and touch; pointer capture keeps the "up" event on
  // the same target even if the finger drifts off the button.
  const holdRef = useRef<{ initial: number | null; interval: number | null }>({
    initial: null,
    interval: null,
  });
  const stopHold = useCallback(() => {
    if (holdRef.current.initial != null) {
      window.clearTimeout(holdRef.current.initial);
      holdRef.current.initial = null;
    }
    if (holdRef.current.interval != null) {
      window.clearInterval(holdRef.current.interval);
      holdRef.current.interval = null;
    }
  }, []);
  const startHold = useCallback((delta: number) => {
    stopHold();
    stepBy(delta);
    holdRef.current.initial = window.setTimeout(() => {
      holdRef.current.interval = window.setInterval(() => stepBy(delta), HOLD_INTERVAL_MS);
    }, HOLD_DELAY_MS);
  }, [stepBy, stopHold]);
  useEffect(() => stopHold, [stopHold]);

  const holdHandlers = (delta: number) => ({
    onPointerDown: (e: React.PointerEvent<HTMLButtonElement>) => {
      if (e.button !== undefined && e.button !== 0) return;
      e.currentTarget.setPointerCapture(e.pointerId);
      startHold(delta);
    },
    onPointerUp: stopHold,
    onPointerCancel: stopHold,
    onLostPointerCapture: stopHold,
  });

  // Keyboard control — works whenever focus is anywhere inside the player.
  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    const isButton = target.tagName === "BUTTON";
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      stepBy(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      stepBy(1);
    } else if (e.key === " " || e.code === "Space") {
      // Native button activation already handles Space; don't double-toggle.
      if (isButton) return;
      e.preventDefault();
      togglePlay();
    }
  };

  // Make the whole player a focus target: tapping the canvas (or any
  // non-interactive area) gives focus so keyboard shortcuts start working.
  const handleRootPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    if (target.closest("button, input, [tabindex]")) return;
    rootRef.current?.focus({ preventScroll: true });
  };

  const handleScrub = (e: React.ChangeEvent<HTMLInputElement>) => {
    setCurrentStep(Number(e.target.value));
    setPlaying(false);
  };

  const handleTimelineClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (totalStepsRef.current <= 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    setCurrentStep(Math.round(ratio * (totalStepsRef.current - 1)));
    setPlaying(false);
  }, []);

  const handleTimelineMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const total = totalStepsRef.current;
    const hl = highlightsRef.current;
    if (total <= 1 || hl.length === 0) { setHoveredMark(null); return; }
    const rect = e.currentTarget.getBoundingClientRect();
    const cursorStep = ((e.clientX - rect.left) / rect.width) * (total - 1);
    const threshold = Math.max(2, total * 0.03);
    let best: Highlight | null = null;
    let bestDist = Infinity;
    for (const h of hl) {
      const d = Math.abs(h.step - cursorStep);
      if (d < bestDist) { bestDist = d; best = h; }
    }
    setHoveredMark(bestDist <= threshold ? best : null);
  }, []);

  // ── Label helpers ────────────────────────────────────────────────────────
  // Highlight markers carry sim snake_ids; translate to seat so labels and
  // colors join consistently with the legend / participantNames.
  const seatForSnake = (snakeIdx: number) => store.seatBySnakeId.get(snakeIdx) ?? snakeIdx;
  const seatLabel = (seat: number) => {
    const name = participantNames[seat] ?? `seat ${seat}`;
    return labelForSeat ? labelForSeat(seat, name) : name;
  };
  const markLabel = (h: Highlight) => {
    const seat = seatForSnake(h.snakeIdx);
    if (h.kind === "death") return `${seatLabel(seat)} died · step ${h.step + 1}`;
    if (h.trappingIdx !== undefined) {
      const trapperSeat = seatForSnake(h.trappingIdx);
      return `${seatLabel(seat)} trapped by ${seatLabel(trapperSeat)} · step ${h.step + 1}`;
    }
    return `${seatLabel(seat)} trapped · step ${h.step + 1}`;
  };

  const overlayText = (() => {
    if (status === "connecting") return "connecting…";
    if (status === "loading")    return "loading replay…";
    if (status === "failed")     return errorMsg || "match failed";
    if (status === "error")      return errorMsg || "failed to load replay";
    return null;
  })();
  const overlayIsError = status === "failed" || status === "error";
  const showControls = totalSteps > 0;
  const execTimes = showExecTimesBar ? store.getExecTimes(currentStep) : null;
  const hasExecTimes = execTimes && Object.keys(execTimes).length > 0;
  const atLatestFrame = currentStep >= totalSteps - 1 && totalSteps > 0;

  return (
    <div
      className="sim-player"
      ref={rootRef}
      tabIndex={0}
      onKeyDown={handleKeyDown}
      onPointerDown={handleRootPointerDown}
    >
      <div className="sim-canvas-wrap" ref={containerRef}>
        <canvas ref={canvasRef} className="sim-canvas" />
        {overlayText && (
          <div className={`sim-overlay${overlayIsError ? " sim-overlay-err" : ""}`}>
            {overlayText}
          </div>
        )}
        {showLiveBadge && status === "live" && atLatestFrame && (
          <div className="sim-live-badge">● LIVE</div>
        )}
      </div>

      {participantNames.length > 0 && (
        <div className="sim-legend">
          {participantNames.map((name, seat) => (
            <span key={seat} className="sim-legend-item">
              <span
                className="sim-legend-dot"
                style={{ background: colorForSeat(seat, participantNames.length).head }}
              />
              {labelForSeat ? labelForSeat(seat, name) : name}
            </span>
          ))}
        </div>
      )}

      {showControls && (
        <div className="sim-controls">
          <button
            className="btn ghost sim-ctrl-btn"
            title="step back (hold to fast-rewind)"
            aria-label="step back"
            {...holdHandlers(-1)}
          >
            ⏮
          </button>
          <button className="btn ghost sim-ctrl-btn" onClick={togglePlay}>
            {playing ? "⏸" : "▶"}
          </button>
          <button
            className="btn ghost sim-ctrl-btn"
            title="step forward (hold to fast-forward)"
            aria-label="step forward"
            {...holdHandlers(1)}
          >
            ⏭
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

      {showExecTimesBar && hasExecTimes && (
        <div className="exec-times-bar">
          <span className="exec-times-label">CPU / step</span>
          {Object.entries(execTimes!).map(([id, ms]) => {
            const seat = Number(id);
            return (
              <span key={id} className="exec-times-entry">
                <span
                  className="exec-times-dot"
                  style={{ background: colorForSeat(seat, participantNames.length).head }}
                />
                <span className="exec-times-name">
                  {labelForSeat ? labelForSeat(seat, participantNames[seat] ?? `snake ${seat}`) : (participantNames[seat] ?? `snake ${seat}`)}
                </span>
                <span className="exec-times-ms">{ms.toFixed(1)}ms</span>
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}
