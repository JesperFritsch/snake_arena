import { useEffect, useState } from "react";
import { useApi, ApiError } from "../api/client";
import type { LanguageInfo } from "../api/types";

type NameStatus =
  | { kind: "empty" }
  | { kind: "checking" }
  | { kind: "available" }
  | { kind: "taken" }
  | { kind: "error"; message: string };

interface Props {
  languages: LanguageInfo[];
  onClose: () => void;
  /** Throws on failure (e.g. a raced duplicate); the dialog shows it inline. */
  onCreate: (name: string, language: string) => Promise<void>;
}

export function NewProjectDialog({ languages, onClose, onCreate }: Props) {
  const api = useApi();

  const [name, setName] = useState("");
  const [language, setLanguage] = useState(languages[0]?.name ?? "");
  const [status, setStatus] = useState<NameStatus>({ kind: "empty" });
  const [busy, setBusy] = useState(false);

  // Default the language once the list is available.
  useEffect(() => {
    if (!language && languages.length) setLanguage(languages[0].name);
  }, [languages, language]);

  // Debounced availability check as the user types (names are globally unique).
  useEffect(() => {
    const trimmed = name.trim();
    if (!trimmed) {
      setStatus({ kind: "empty" });
      return;
    }
    setStatus({ kind: "checking" });
    let cancelled = false;
    const t = setTimeout(() => {
      api
        .checkNameAvailable(trimmed)
        .then((res) => {
          if (!cancelled) setStatus(res.available ? { kind: "available" } : { kind: "taken" });
        })
        .catch((e) => {
          if (!cancelled) setStatus({ kind: "error", message: e instanceof ApiError ? e.detail : String(e) });
        });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [name, api]);

  const canCreate = status.kind === "available" && !!language && !busy;

  const create = async () => {
    if (!canCreate) return;
    setBusy(true);
    try {
      await onCreate(name.trim(), language);
      onClose();
    } catch (e) {
      setStatus({ kind: "error", message: e instanceof ApiError ? e.detail : String(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="title">New project</span>
          <button className="btn ghost" style={{ padding: "2px 8px" }} onClick={onClose}>✕</button>
        </div>

        <div className="modal-body">
          <div className="form-row">
            <label>Name</label>
            <input
              className="input"
              autoFocus
              value={name}
              placeholder="my-snake"
              style={{ flex: 1 }}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void create(); }}
            />
          </div>
          <div style={{ fontSize: 11, minHeight: 16, marginTop: 4, marginLeft: 2 }}>
            {status.kind === "empty" && <span className="muted">must be unique across all users</span>}
            {status.kind === "checking" && <span className="muted">checking…</span>}
            {status.kind === "available" && <span style={{ color: "#3fb950" }}>✓ available</span>}
            {status.kind === "taken" && <span style={{ color: "var(--red)" }}>✗ name already taken</span>}
            {status.kind === "error" && <span style={{ color: "var(--red)" }}>{status.message}</span>}
          </div>

          <div className="form-row" style={{ marginTop: 12 }}>
            <label>Language</label>
            <select className="select" value={language} onChange={(e) => setLanguage(e.target.value)}>
              {languages.length === 0 && <option value="">no languages available</option>}
              {languages.map((l) => (
                <option key={l.name} value={l.name}>
                  {l.name}{l.version ? ` (${l.version})` : ""}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="modal-foot">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!canCreate} onClick={create}>
            {busy ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
