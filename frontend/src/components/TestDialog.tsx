import { useEffect, useMemo, useState } from "react";
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
  gridWidth: String(MIN_GRID),
  gridHeight: String(MIN_GRID),
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
  const [counts, setCounts]           = useState<Record<number, number>>(() => {
    const c: Record<number, number> = {};
    for (const id of init.opponentIds) c[id] = (c[id] ?? 0) + 1;
    return c;
  });
  const [search, setSearch]           = useState("");
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

  const totalSelected = Object.values(counts).reduce((a, b) => a + b, 0);

  const { minFood, maxFood } = useMemo(() => {
    let w: number, h: number;
    if (arenaMode === "map") {
      const map = maps.find((m) => m.name === selectedMap);
      if (!map) return { minFood: 1, maxFood: 1 };
      ({ width: w, height: h } = map);
    } else {
      w = parseInt(gridWidth);
      h = parseInt(gridHeight);
      if (isNaN(w) || isNaN(h)) return { minFood: 1, maxFood: 1 };
    }
    const min = Math.max(1, Math.floor((w * h) / 50));
    // leave a cell for each snake (player + opponents) at spawn
    const max = Math.max(min, w * h - (totalSelected + 1));
    return { minFood: min, maxFood: max };
  }, [arenaMode, gridWidth, gridHeight, selectedMap, maps, totalSelected]);

  useEffect(() => {
    setFood((f) => Math.min(maxFood, Math.max(minFood, f)));
  }, [minFood, maxFood]);

  const adjust = (id: number, delta: number) => {
    setCounts((prev) => {
      const total = Object.values(prev).reduce((a, b) => a + b, 0);
      if (delta > 0 && total >= MAX_OPPONENTS) return prev;
      const next = Math.max(0, (prev[id] ?? 0) + delta);
      return { ...prev, [id]: next };
    });
  };

  const onGridChange = (set: (v: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) => {
    set(e.target.value.replace(/\D/g, ""));
  };

  const clampGrid = (set: (v: string) => void) => (e: React.FocusEvent<HTMLInputElement>) => {
    const n = parseInt(e.target.value);
    if (isNaN(n)) { set(String(MIN_GRID)); return; }
    set(String(Math.min(MAX_GRID, Math.max(MIN_GRID, n))));
  };

  const filteredOpponents = opponents.filter((p) => {
    const q = search.toLowerCase();
    return p.name.toLowerCase().includes(q) || p.user_display_name.toLowerCase().includes(q);
  });

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

    const opponentIds = Object.entries(counts).flatMap(([id, n]) => Array<number>(n).fill(Number(id)));
    const settings: TestSettings = {
      food,
      arenaMode,
      gridWidth,
      gridHeight,
      map: arenaMode === "map" ? selectedMap : null,
      opponentIds,
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
            Opponents <span className="muted">({totalSelected}/{MAX_OPPONENTS} selected)</span>
          </div>
          {loading ? (
            <span className="muted">loading…</span>
          ) : (
            <>
              {opponents.length > 4 && (
                <input
                  className="input"
                  style={{ width: "100%", marginBottom: 6, boxSizing: "border-box" }}
                  placeholder="Search agents…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              )}
              <div className="check-list">
                {filteredOpponents.map((p) => {
                  const count = counts[p.id] ?? 0;
                  const atMax = totalSelected >= MAX_OPPONENTS;
                  return (
                    <div key={p.id} className="check-row">
                      <span className="check-name">{p.name}</span>
                      <span className="muted" style={{ flex: 1 }}>{p.user_display_name} · {fmtLang(p.language, languages)} · v{p.submitted_version}</span>
                      <div className="stepper">
                        <button className="stepper-btn" onClick={() => adjust(p.id, -1)} disabled={count === 0}>−</button>
                        <span className="stepper-count">{count}</span>
                        <button className="stepper-btn" onClick={() => adjust(p.id, +1)} disabled={atMax}>+</button>
                      </div>
                    </div>
                  );
                })}
                {filteredOpponents.length === 0 && search && (
                  <span className="muted">No results for "{search}".</span>
                )}
                {opponents.length === 0 && (
                  <span className="muted">No submitted projects available yet.</span>
                )}
              </div>
            </>
          )}

          <div className="modal-section-label" style={{ marginTop: 16 }}>Sim settings</div>
          <div className="form-row">
            <label>Food</label>
            <input
              className="input"
              type="number"
              inputMode="numeric"
              min={minFood}
              max={maxFood}
              value={food}
              style={{ width: 70 }}
              onChange={(e) => setFood(parseInt(e.target.value.replace(/\D/g, "")) || minFood)}
              onBlur={(e) => {
                const n = parseInt(e.target.value);
                setFood(isNaN(n) ? minFood : Math.min(maxFood, Math.max(minFood, n)));
              }}
            />
            {minFood > 1 && <span className="muted" style={{ fontSize: 11 }}>min {minFood}</span>}
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
                inputMode="numeric"
                placeholder="width"
                value={gridWidth}
                min={MIN_GRID}
                max={MAX_GRID}
                style={{ width: 80 }}
                onChange={onGridChange(setGridWidth)}
                onBlur={clampGrid(setGridWidth)}
              />
              <span className="muted">×</span>
              <input
                className="input"
                type="number"
                inputMode="numeric"
                placeholder="height"
                value={gridHeight}
                min={MIN_GRID}
                max={MAX_GRID}
                style={{ width: 80 }}
                onChange={onGridChange(setGridHeight)}
                onBlur={clampGrid(setGridHeight)}
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
