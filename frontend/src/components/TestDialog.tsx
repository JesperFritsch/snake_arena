import { useEffect, useState } from "react";
import { useApi, ApiError } from "../api/client";
import type { ProjectMeta, PublicProjectSummary, TestMatchJob } from "../api/types";
import { useToast } from "./Toast";

const MAX_OPPONENTS = 4;

interface Props {
  project: ProjectMeta;
  onClose: () => void;
  onEnqueued: (job: TestMatchJob) => void;
}

export function TestDialog({ project, onClose, onEnqueued }: Props) {
  const api = useApi();
  const { push } = useToast();

  const [opponents, setOpponents] = useState<PublicProjectSummary[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [food, setFood] = useState(3);
  const [gridWidth, setGridWidth] = useState("");
  const [gridHeight, setGridHeight] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .listOpponents()
      .then((all) => setOpponents(all.filter((p) => p.id !== project.id)))
      .catch((e) => push(`Could not load opponents: ${e instanceof ApiError ? e.detail : e}`, "error"))
      .finally(() => setLoading(false));
  }, []);

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else if (next.size < MAX_OPPONENTS) {
        next.add(id);
      }
      return next;
    });
  };

  const run = async () => {
    const w = parseInt(gridWidth);
    const h = parseInt(gridHeight);
    const hasW = !isNaN(w) && gridWidth !== "";
    const hasH = !isNaN(h) && gridHeight !== "";
    if (hasW !== hasH) {
      push("Provide both grid width and height, or leave both empty.", "error");
      return;
    }
    setBusy(true);
    try {
      const sim_args: { food: number; grid_width?: number; grid_height?: number } = { food };
      if (hasW && hasH) { sim_args.grid_width = w; sim_args.grid_height = h; }
      const job = await api.enqueueTestMatch({
        player_project_id: project.id,
        opponent_project_ids: [...selected],
        sim_args,
      });
      onEnqueued(job);
      onClose();
    } catch (e) {
      push(`Could not start test match: ${e instanceof ApiError ? e.detail : e}`, "error");
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
          ) : opponents.length === 0 ? (
            <span className="muted">No submitted projects available yet.</span>
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
                    <span className="muted">{p.user_display_name} · {p.language} · v{p.submitted_version}</span>
                  </label>
                );
              })}
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
