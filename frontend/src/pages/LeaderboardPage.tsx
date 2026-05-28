import { useCallback, useEffect, useRef, useState } from "react";
import { useApi } from "../api/client";
import { RankedMatchPlayer } from "../components/RankedMatchPlayer";
import type {
  LeaderboardEntry,
  Mode,
  OverallLeaderboardEntry,
  RankedMatchParticipant,
  RankedMatchSummary,
} from "../api/types";

const LANG_COLORS: Record<string, string> = {
  python: "var(--blue)",
  javascript: "var(--amber)",
  java: "var(--red)",
  rust: "#ff8c42",
  go: "#79d4f7",
};

// "overall" pseudo-tab — anything else is a mode slug
type Tab = "overall" | string;

function LangBadge({ lang }: { lang: string }) {
  const color = LANG_COLORS[lang.toLowerCase()] ?? "var(--text-faint)";
  return (
    <span
      style={{
        fontSize: 10,
        letterSpacing: "0.6px",
        textTransform: "uppercase",
        color,
        border: `1px solid ${color}`,
        borderRadius: 3,
        padding: "1px 5px",
        opacity: 0.85,
        flexShrink: 0,
      }}
    >
      {lang}
    </span>
  );
}

function RankMedal({ rank }: { rank: number }) {
  if (rank === 1) return <span style={{ color: "#ffd700" }}>▲</span>;
  if (rank === 2) return <span style={{ color: "#c0c0c0" }}>▲</span>;
  if (rank === 3) return <span style={{ color: "#cd7f32" }}>▲</span>;
  return null;
}

function fmt(n: number, decimals = 1) {
  return n.toFixed(decimals);
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function SurvivalBadge({ rank, total }: { rank: number | null; total: number }) {
  if (rank === null) return <span className="lb-match-rank muted">—</span>;
  const color =
    rank === 1 ? "#ffd700" :
    rank === 2 ? "#c0c0c0" :
    rank === 3 ? "#cd7f32" :
    "var(--text-faint)";
  return (
    <span className="lb-match-rank" style={{ color }}>#{rank}/{total}</span>
  );
}

// ── LeaderboardPage ──────────────────────────────────────────────────────────

export function LeaderboardPage() {
  const api = useApi();
  const [modes, setModes] = useState<Mode[] | null>(null);
  const [tab, setTab] = useState<Tab>("overall");

  const [overall, setOverall] = useState<OverallLeaderboardEntry[] | null>(null);
  const [modeEntries, setModeEntries] = useState<LeaderboardEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Project-detail modal (uses same shape as before but works for ranked or overall rows)
  const [selectedProject, setSelectedProject] = useState<{ id: number; name: string; language: string; user_display_name: string } | null>(null);
  const [matchList, setMatchList] = useState<RankedMatchSummary[] | null>(null);
  const [matchListLoading, setMatchListLoading] = useState(false);
  const [selectedMatch, setSelectedMatch] = useState<RankedMatchSummary | null>(null);

  // Load modes once.
  useEffect(() => {
    let cancelled = false;
    api.getModes()
      .then((data) => { if (!cancelled) setModes(data); })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, []);

  // Load the current tab's data whenever it changes.
  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (tab === "overall") {
      setOverall(null);
      api.getOverallLeaderboard(100)
        .then((data) => { if (!cancelled) setOverall(data); })
        .catch((e) => { if (!cancelled) setError(String(e)); });
    } else {
      setModeEntries(null);
      api.getModeLeaderboard(tab, 100)
        .then((data) => { if (!cancelled) setModeEntries(data); })
        .catch((e) => { if (!cancelled) setError(String(e)); });
    }
    return () => { cancelled = true; };
  }, [tab]); // eslint-disable-line react-hooks/exhaustive-deps

  const closeModal = useCallback(() => {
    setSelectedProject(null);
    setMatchList(null);
    setSelectedMatch(null);
  }, []);

  // When a project is selected, load its match history.
  useEffect(() => {
    if (!selectedProject) return;
    let cancelled = false;
    setMatchList(null);
    setSelectedMatch(null);
    setMatchListLoading(true);
    api.listProjectRankedMatches(selectedProject.id, 50)
      .then((data) => { if (!cancelled) { setMatchList(data); setMatchListLoading(false); } })
      .catch(() => { if (!cancelled) { setMatchList([]); setMatchListLoading(false); } });
    return () => { cancelled = true; };
  }, [selectedProject?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Escape closes the modal.
  useEffect(() => {
    if (!selectedProject) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeModal(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedProject, closeModal]);

  const getBundleUrl = useCallback(
    () => api.getMatchBundleUrl(selectedMatch!.id),
    [api, selectedMatch?.id],
  );

  const activeMode = modes?.find((m) => m.slug === tab) ?? null;

  return (
    <div className="page-pad">
      <div className="lead">
        <h1>Leaderboard</h1>
        <p className="sub">The best snakes in the arena.</p>

        {/* Tabs */}
        {modes && modes.length > 0 && (
          <div className="lb-tabs">
            <button
              className={`lb-tab${tab === "overall" ? " active" : ""}`}
              onClick={() => setTab("overall")}
            >
              Overall
            </button>
            {modes.map((m) => (
              <button
                key={m.slug}
                className={`lb-tab${tab === m.slug ? " active" : ""}`}
                onClick={() => setTab(m.slug)}
                title={m.description ?? undefined}
              >
                {m.name}
                <span className="lb-tab-sub">{m.participant_count}p</span>
              </button>
            ))}
          </div>
        )}

        {error && (
          <div style={{ color: "var(--red)", marginBottom: 24, fontSize: 13 }}>
            Failed to load: {error}
          </div>
        )}

        {/* Overall view */}
        {tab === "overall" && (
          <OverallTable
            entries={overall}
            error={error}
            onSelectProject={setSelectedProject}
          />
        )}

        {/* Per-mode view */}
        {tab !== "overall" && (
          <ModeTable
            entries={modeEntries}
            error={error}
            mode={activeMode}
            onSelectProject={setSelectedProject}
          />
        )}
      </div>

      {selectedProject && (
        <MatchModal
          project={selectedProject}
          matchList={matchList}
          matchListLoading={matchListLoading}
          selectedMatch={selectedMatch}
          onSelectMatch={setSelectedMatch}
          getBundleUrl={getBundleUrl}
          onClose={closeModal}
          modes={modes}
        />
      )}
    </div>
  );
}

// ── Overall table ────────────────────────────────────────────────────────────

interface OverallTableProps {
  entries: OverallLeaderboardEntry[] | null;
  error: string | null;
  onSelectProject: (p: { id: number; name: string; language: string; user_display_name: string }) => void;
}

function OverallTable({ entries, error, onSelectProject }: OverallTableProps) {
  if (entries === null && !error) {
    return <div style={{ color: "var(--text-faint)", fontSize: 13 }}>Loading…</div>;
  }
  if (entries !== null && entries.length === 0) {
    return (
      <div className="placeholder-card">
        <div className="big">no one qualifies yet</div>
        <div>
          The overall board only shows projects that have played at least half
          the target matches in every enabled mode. Keep playing — your agent
          will appear once the scheduler has worked through all of them.
        </div>
      </div>
    );
  }
  if (entries === null) return null;

  return (
    <>
      <table className="lb-table">
        <thead>
          <tr>
            <th className="lb-th lb-rank">#</th>
            <th className="lb-th lb-agent">Agent</th>
            <th className="lb-th lb-author">Author</th>
            <th className="lb-th lb-num" title="Mean of per-mode scores normalised to the mode leader (0–100)">
              Overall
            </th>
            <th className="lb-th lb-num" title="Total ranked matches across all modes">Matches</th>
            <th className="lb-th lb-num" title="How many modes this agent competes in">Modes</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr
              key={e.project_id}
              className={`lb-row lb-row-clickable ${e.rank <= 3 ? "lb-row-top" : ""}`}
              onClick={() =>
                onSelectProject({
                  id: e.project_id,
                  name: e.project_name,
                  language: e.language,
                  user_display_name: e.user_display_name,
                })
              }
            >
              <td className="lb-td lb-rank">
                <span className="lb-rank-num">{e.rank}</span>
                <RankMedal rank={e.rank} />
              </td>
              <td className="lb-td lb-agent">
                <span className="lb-name">{e.project_name}</span>
                <LangBadge lang={e.language} />
              </td>
              <td className="lb-td lb-author">{e.user_display_name}</td>
              <td className="lb-td lb-num lb-score">{fmt(e.overall_score, 1)}</td>
              <td className="lb-td lb-num">{e.total_matches}</td>
              <td className="lb-td lb-num">{e.modes_played}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="lb-footnote">
        Overall score normalises each mode's avg_score to 0–100 (relative to that
        mode's leader) and averages across modes. Eligible only if you've played
        ≥ half the target matches in every enabled mode.
      </p>
    </>
  );
}

// ── Per-mode table ───────────────────────────────────────────────────────────

interface ModeTableProps {
  entries: LeaderboardEntry[] | null;
  error: string | null;
  mode: Mode | null;
  onSelectProject: (p: { id: number; name: string; language: string; user_display_name: string }) => void;
}

function ModeTable({ entries, error, mode, onSelectProject }: ModeTableProps) {
  if (entries === null && !error) {
    return <div style={{ color: "var(--text-faint)", fontSize: 13 }}>Loading…</div>;
  }
  if (entries !== null && entries.length === 0) {
    return (
      <div className="placeholder-card">
        <div className="big">no matches yet</div>
        <div>The scheduler hasn't run scored matches in this mode yet.</div>
      </div>
    );
  }
  if (entries === null) return null;

  return (
    <>
      {mode?.description && (
        <p className="lb-mode-desc">
          {mode.description}{" "}
          <span className="muted">
            (target {mode.target_matches_per_version} matches per version, {mode.budget_ms.toFixed(0)} ms budget)
          </span>
        </p>
      )}
      <table className="lb-table">
        <thead>
          <tr>
            <th className="lb-th lb-rank">#</th>
            <th className="lb-th lb-agent">Agent</th>
            <th className="lb-th lb-author">Author</th>
            <th className="lb-th lb-num" title="Average score in this mode">Avg score</th>
            <th className="lb-th lb-num" title="Highest single-match score">Best</th>
            <th className="lb-th lb-num" title="Average final snake length">Avg length</th>
            <th className="lb-th lb-num" title="Average survival rank (1 = survived longest)">Avg rank</th>
            <th className="lb-th lb-num">Matches</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr
              key={e.project_id}
              className={`lb-row lb-row-clickable ${e.rank <= 3 ? "lb-row-top" : ""}`}
              onClick={() =>
                onSelectProject({
                  id: e.project_id,
                  name: e.project_name,
                  language: e.language,
                  user_display_name: e.user_display_name,
                })
              }
            >
              <td className="lb-td lb-rank">
                <span className="lb-rank-num">{e.rank}</span>
                <RankMedal rank={e.rank} />
              </td>
              <td className="lb-td lb-agent">
                <span className="lb-name">{e.project_name}</span>
                <LangBadge lang={e.language} />
              </td>
              <td className="lb-td lb-author">{e.user_display_name}</td>
              <td className="lb-td lb-num lb-score">{fmt(e.avg_score, 2)}</td>
              <td className="lb-td lb-num">{fmt(e.best_score, 2)}</td>
              <td className="lb-td lb-num">
                {e.avg_length != null ? fmt(e.avg_length, 1) : "—"}
              </td>
              <td className="lb-td lb-num">{fmt(e.avg_rank, 2)}</td>
              <td className="lb-td lb-num">{e.matches_played}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="lb-footnote">
        Score = length × (1 + β·length/steps) × (1 + α·(1 − (rank−1)/(n−1))) × (budget/avg_step_ms)^w.
        Click a row to view this agent's match replays.
      </p>
    </>
  );
}

// ── Match modal ──────────────────────────────────────────────────────────────

interface MatchModalProps {
  project: { id: number; name: string; language: string; user_display_name: string };
  matchList: RankedMatchSummary[] | null;
  matchListLoading: boolean;
  selectedMatch: RankedMatchSummary | null;
  onSelectMatch: (m: RankedMatchSummary) => void;
  getBundleUrl: () => Promise<{ url: string }>;
  onClose: () => void;
  modes: Mode[] | null;
}

function MatchModal({
  project,
  matchList,
  matchListLoading,
  selectedMatch,
  onSelectMatch,
  getBundleUrl,
  onClose,
  modes,
}: MatchModalProps) {
  const overlayRef = useRef<HTMLDivElement>(null);
  const onOverlay = (e: React.MouseEvent) => {
    if (e.target === overlayRef.current) onClose();
  };

  const modeName = (mid: number | null): string => {
    if (mid == null) return "test";
    return modes?.find((m) => m.id === mid)?.name ?? `mode #${mid}`;
  };

  return (
    <div className="modal-overlay" ref={overlayRef} onClick={onOverlay}>
      <div className="lb-match-modal">
        <div className="modal-head">
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontWeight: 600 }}>{project.name}</span>
            <LangBadge lang={project.language} />
            <span style={{ color: "var(--text-faint)", fontSize: 13 }}>
              by {project.user_display_name}
            </span>
          </div>
          <button className="btn ghost" onClick={onClose} style={{ padding: "2px 8px" }}>×</button>
        </div>

        <div className="lb-match-body">
          <div className="lb-match-list">
            {matchListLoading && <div className="lb-match-empty">Loading…</div>}
            {!matchListLoading && matchList !== null && matchList.length === 0 && (
              <div className="lb-match-empty">No ranked matches yet.</div>
            )}
            {matchList?.map((m) => {
              const me = m.participants.find((p) => p.project_id === project.id);
              const others = m.participants
                .filter((p) => p.project_id !== project.id)
                .map((p) => p.project_name);
              const score = me?.metrics?.score as number | undefined;
              return (
                <MatchListRow
                  key={m.id}
                  match={m}
                  me={me ?? null}
                  opponents={others}
                  score={score}
                  modeLabel={modeName(m.mode_id)}
                  active={selectedMatch?.id === m.id}
                  onClick={() => onSelectMatch(m)}
                />
              );
            })}
          </div>

          <div className="lb-match-player">
            {selectedMatch ? (
              <RankedMatchPlayer
                key={selectedMatch.id}
                match={selectedMatch}
                getBundleUrl={getBundleUrl}
              />
            ) : (
              <div className="lb-match-player-placeholder">
                {matchList && matchList.length > 0
                  ? "Select a match to watch the replay"
                  : matchListLoading
                  ? ""
                  : "No replays available"}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

interface MatchListRowProps {
  match: RankedMatchSummary;
  me: RankedMatchParticipant | null;
  opponents: string[];
  score: number | undefined;
  modeLabel: string;
  active: boolean;
  onClick: () => void;
}

function MatchListRow({ match, me, opponents, score, modeLabel, active, onClick }: MatchListRowProps) {
  const total = match.participants.length;
  const oStr  = opponents.length > 0
    ? `vs ${opponents.slice(0, 2).join(", ")}${opponents.length > 2 ? ` +${opponents.length - 2}` : ""}`
    : "";
  return (
    <div className={`lb-match-item${active ? " active" : ""}`} onClick={onClick} role="button">
      <div className="lb-match-item-top">
        <span className="lb-match-date">{formatDate(match.started_at)}</span>
        <SurvivalBadge rank={me?.survival_rank ?? null} total={total} />
        {score !== undefined && (
          <span className="lb-match-score">{score.toFixed(1)}</span>
        )}
      </div>
      <div className="lb-match-opponents">
        <span className="lb-match-mode">{modeLabel}</span>
        {oStr && <span style={{ marginLeft: 6 }}>· {oStr}</span>}
      </div>
    </div>
  );
}
