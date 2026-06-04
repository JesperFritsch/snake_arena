import { useEffect, useState } from "react";
import { useApi, ApiError, BASE_URL } from "../api/client";
import type { LanguageInfo, MapInfo, ProjectMeta, PublicProjectSummary, QuotaStatus } from "../api/types";
import { fmtLang } from "../lib/editor";
import { QuotaIndicator } from "./QuotaIndicator";
import { useToast } from "./Toast";

const MAX_OPPONENTS = 4;
const MIN_GRID = 5;
const MAX_GRID = 20;

export interface TestSettings {
  food: number;
  arenaMode: "custom" | "map";
  gridWidth: string;
  gridHeight: string;
  map: string | null;
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

const DEFAULT_SETTINGS: TestSettings = {
  food: 3,
  arenaMode: "custom",
  gridWidth: "",
  gridHeight: "",
  map: null,
  opponentIds: [],
};

interface Props {
  project: ProjectMeta;
  initialSettings: TestSettings | null;
  languages: LanguageInfo[];
  quota: QuotaStatus | null;
  onClose: () => void;
  onRun: (settings: TestSettings) => Promise<void>;
}

export function TestDialog({ project, initialSettings, languages, quota, onClose, onRun }: Props) {
  const api = useApi();
  const { push } = useToast();

  const init = initialSettings ?? DEFAULT_SETTINGS;

  const [opponents, setOpponents]     = useState<PublicProjectSummary[]>([]);
  const [maps, setMaps]               = useState<MapInfo[]>([]);
  const [selected, setSelected]       = useState<Set<number>>(new Set(init.opponentIds));
  const [food, setFood]               = useState(init.food);
  const [arenaMode, setArenaMode]     = useState<"custom" | "map">(init.arenaMode ?? "custom");
  const [gridWidth, setGridWidth]     = useState(init.gridWidth);
  const [gridHeight, setGridHeight]   = useState(init.gridHeight);
  const [selectedMap, setSelectedMap] = useState<string | null>(init.map ?? null);
  const [loading, setLoading]         = useState(true);
  const [busy, setBusy]               = useState(false);

  useEffect(() => {
    Promise.all([
      api.listOpponents(),
      api.listMaps(),
    ])
      .then(([opps, mapList]) => {
        setOpponents(opps);
        setMaps(mapList);
      })
      .catch((e) => push(`Could not load data: ${e instanceof ApiError ? e.detail : e}`, "error"))
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
    if (arenaMode === "custom") {
      const w = parseInt(gridWidth);
      const h = parseInt(gridHeight);
      const hasW = gridWidth !== "" && !isNaN(w);
      const hasH = gridHeight !== "" && !isNaN(h);
      if (!hasW || !hasH) {
        push("Enter both grid width and height (5–20).", "error");
        return;
      }
      if (w < MIN_GRID || h < MIN_GRID || w > MAX_GRID || h > MAX_GRID) {
        push(`Grid must be between ${MIN_GRID}×${MIN_GRID} and ${MAX_GRID}×${MAX_GRID}.`, "error");
        return;
      }
    } else {
      if (!selectedMap) {
        push("Select a map.", "error");
        return;
      }
    }

    const settings: TestSettings = {
      food,
      arenaMode,
      gridWidth,
      gridHeight,
      map: arenaMode === "map" ? selectedMap : null,
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

  const outOfQuota = quota != null && quota.remaining === 0;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
            <span className="title">Test match — {project.name}</span>
            <QuotaIndicator status={quota} label="tests/hr" />
          </span>
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

          <div className="form-row" style={{ marginTop: 10 }}>
            <div className="seg-ctrl">
              <button
                className={`seg-btn${arenaMode === "custom" ? " active" : ""}`}
                onClick={() => setArenaMode("custom")}
              >
                Custom size
              </button>
              <button
                className={`seg-btn${arenaMode === "map" ? " active" : ""}`}
                onClick={() => setArenaMode("map")}
              >
                Map
              </button>
            </div>
          </div>

          {arenaMode === "custom" && (
            <div className="form-row" style={{ marginTop: 8 }}>
              <label>Grid</label>
              <input
                className="input"
                type="number"
                placeholder="width"
                value={gridWidth}
                min={MIN_GRID}
                max={MAX_GRID}
                style={{ width: 80 }}
                onChange={(e) => setGridWidth(e.target.value)}
              />
              <span className="muted">×</span>
              <input
                className="input"
                type="number"
                placeholder="height"
                value={gridHeight}
                min={MIN_GRID}
                max={MAX_GRID}
                style={{ width: 80 }}
                onChange={(e) => setGridHeight(e.target.value)}
              />
              <span className="muted" style={{ fontSize: 11 }}>{MIN_GRID}–{MAX_GRID}</span>
            </div>
          )}

          {arenaMode === "map" && (
            <div className="map-picker" style={{ marginTop: 8 }}>
              {loading ? (
                <span className="muted">loading…</span>
              ) : maps.length === 0 ? (
                <span className="muted">No maps available.</span>
              ) : (
                maps.map((m) => (
                  <button
                    key={m.name}
                    className={`map-thumb${selectedMap === m.name ? " selected" : ""}`}
                    onClick={() => setSelectedMap(m.name)}
                    title={`${m.name} (${m.width}×${m.height})`}
                  >
                    <img
                      src={`${BASE_URL}/maps/${encodeURIComponent(m.name)}/image`}
                      alt={m.name}
                      style={{ imageRendering: "pixelated", display: "block", width: "100%", height: "100%", objectFit: "contain" }}
                    />
                  </button>
                ))
              )}
            </div>
          )}
        </div>

        <div className="modal-foot">
          {outOfQuota && (
            <span style={{ fontSize: 12, color: "var(--red)", marginRight: "auto" }}>
              Hourly limit reached.{" "}
              {quota?.next_slot_at != null && (
                <>Next slot at {new Date(quota.next_slot_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.</>
              )}
            </span>
          )}
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || loading || outOfQuota} onClick={run}>
            {busy ? "Starting…" : "Run"}
          </button>
        </div>
      </div>
    </div>
  );
}
