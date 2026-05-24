import { useEffect, useState } from "react";
import { useApi, ApiError } from "../api/client";
import type { LanguageInfo, ProjectMeta, PublicProjectSummary } from "../api/types";
import { fmtLang } from "../lib/editor";
import { useToast } from "./Toast";

const MAX_OPPONENTS = 4;

export interface TestSettings {
  food: number;
  gridWidth: string;
  gridHeight: string;
  opponentIds: number[];
}

const LS_KEY = (pid: number) => `snake-arena:test-settings:${pid}`;

export function loadTestSettings(pid: number): TestSettings | null {
  try { return JSON.parse(localStorage.getItem(LS_KEY(pid)) ?? "null"); }
  catch { return null; }
}

export function saveTestSettings(pid: number, s: TestSettings): void {
  localStorage.setItem(LS_KEY(pid), JSON.stringify(s));
}

const DEFAULT_SETTINGS: TestSettings = { food: 3, gridWidth: "", gridHeight: "", opponentIds: [] };

interface Props {
  project: ProjectMeta;
  initialSettings: TestSettings | null;
  languages: LanguageInfo[];
  onClose: () => void;
  onRun: (settings: TestSettings) => Promise<void>;
}

export function TestDialog({ project, initialSettings, languages, onClose, onRun }: Props) {
  const api = useApi();
  const { push } = useToast();

  const init = initialSettings ?? DEFAULT_SETTINGS;

  const [opponents, setOpponents]   = useState<PublicProjectSummary[]>([]);
  const [selected, setSelected]     = useState<Set<number>>(new Set(init.opponentIds));
  const [food, setFood]             = useState(init.food);
  const [gridWidth, setGridWidth]   = useState(init.gridWidth);
  const [gridHeight, setGridHeight] = useState(init.gridHeight);
  const [loading, setLoading]       = useState(true);
  const [busy, setBusy]             = useState(false);

  useEffect(() => {
    api
      .listOpponents()
      .then(setOpponents)
      .catch((e) => push(`Could not load opponents: ${e instanceof ApiError ? e.detail : e}`, "error"))
      .finally(() => setLoading(false));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else if (next.size < MAX_OPPONENTS) next.add(id);
      return next;
    });
  };

  const run = async () => {
    const w = parseInt(gridWidth);
    const h = parseInt(gridHeight);
    const hasW = gridWidth !== "" && !isNaN(w);
    const hasH = gridHeight !== "" && !isNaN(h);
    if (hasW !== hasH) {
      push("Provide both grid width and height, or leave both empty.", "error");
      return;
    }
    const settings: TestSettings = {
      food,
      gridWidth,
      gridHeight,
      opponentIds: [...selected],
    };
    setBusy(true);
    try {
      await onRun(settings);
      onClose();
    } catch {
      // error already toasted by onRun
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="title">Test match — {project.name}</span>
          <button className="btn ghost" style={{ padding: "2px 8px" }} onClick={onClose}>✕</button>
        </div>

        <div className="modal-body">
          <div className="modal-section-label">
            Opponents <span className="muted">({selected.size}/{MAX_OPPONENTS} selected)</span>
          </div>
          {loading ? (
            <span className="muted">loading…</span>
          ) : (
            <div className="check-list">
              {opponents.map((p) => {
                const checked = selected.has(p.id);
                const disabled = !checked && selected.size >= MAX_OPPONENTS;
                return (
                  <label key={p.id} className={`check-row${disabled ? " disabled" : ""}`}>
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={disabled}
                      onChange={() => toggle(p.id)}
                    />
                    <span className="check-name">{p.name}</span>
                    <span className="muted">{p.user_display_name} · {fmtLang(p.language, languages)} · v{p.submitted_version}</span>
                  </label>
                );
              })}
              {[...selected]
                .filter((id) => !opponents.some((o) => o.id === id))
                .map((id) => (
                  <label key={id} className="check-row">
                    <input type="checkbox" checked onChange={() => toggle(id)} />
                    <span className="check-name muted">project #{id}</span>
                    <span className="muted" style={{ color: "var(--red)" }}>not found — uncheck to remove</span>
                  </label>
                ))}
              {!loading && opponents.length === 0 && selected.size === 0 && (
                <span className="muted">No submitted projects available yet.</span>
              )}
            </div>
          )}

          <div className="modal-section-label" style={{ marginTop: 16 }}>Sim settings</div>
          <div className="form-row">
            <label>Food</label>
            <input
              className="input"
              type="number"
              min={1}
              value={food}
              style={{ width: 70 }}
              onChange={(e) => setFood(Math.max(1, parseInt(e.target.value) || 1))}
            />
          </div>
          <div className="form-row">
            <label>Grid</label>
            <input
              className="input"
              type="number"
              placeholder="width"
              value={gridWidth}
              min={5}
              style={{ width: 80 }}
              onChange={(e) => setGridWidth(e.target.value)}
            />
            <span className="muted">×</span>
            <input
              className="input"
              type="number"
              placeholder="height"
              value={gridHeight}
              min={5}
              style={{ width: 80 }}
              onChange={(e) => setGridHeight(e.target.value)}
            />
            <span className="muted" style={{ fontSize: 11 }}>leave empty for sim default</span>
          </div>
        </div>

        <div className="modal-foot">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || loading} onClick={run}>
            {busy ? "Starting…" : "Run"}
          </button>
        </div>
      </div>
    </div>
  );
}
