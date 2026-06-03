import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApi } from "../api/client";
import { BundleSimPlayer } from "../components/BundleSimPlayer";
import type {
  GroupLeaderboardEntry,
  LeaderboardEntry,
  Mode,
  ModeGroup,
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

// One tab per leaderboard view.
//   - "overall"           — cross-group normalised
//   - { kind: "group", slug }      — a mode_group; aggregated across its modes
//   - { kind: "mode",  slug }      — a single mode (ungrouped, OR a sub-tab inside a group)
type Tab =
  | { kind: "overall" }
  | { kind: "group"; slug: string }
  | { kind: "mode"; slug: string };

const OVERALL_TAB: Tab = { kind: "overall" };

function tabKey(t: Tab): string {
  if (t.kind === "overall") return "overall";
  return `${t.kind}:${t.slug}`;
}

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

// Stats shown at the top of the match modal — shape depends on which
// leaderboard view the row was clicked from.
type SelectedRowStats =
  | { kind: "overall"; overall_score: number; total_matches: number; modes_played: number }
  | { kind: "group"; group_score: number; matches_played: number; modes_played: number; mode_count: number }
  | { kind: "mode"; score: number; category_breakdown: Record<string, { raw: number; rank?: number }>; matches_played: number };

type SelectedProject = {
  id: number;
  name: string;
  language: string;
  user_display_name: string;
};

// ── LeaderboardPage ──────────────────────────────────────────────────────────

export function LeaderboardPage() {
  const api = useApi();
  const [modes, setModes] = useState<Mode[] | null>(null);
  const [groups, setGroups] = useState<ModeGroup[] | null>(null);
  const [tab, setTab] = useState<Tab>(OVERALL_TAB);

  const [overall, setOverall] = useState<OverallLeaderboardEntry[] | null>(null);
  const [groupEntries, setGroupEntries] = useState<GroupLeaderboardEntry[] | null>(null);
  const [modeEntries, setModeEntries] = useState<LeaderboardEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Project-detail modal (uses same shape as before but works for ranked or overall rows)
  const [selectedProject, setSelectedProject] = useState<SelectedProject | null>(null);
  const [selectedStats, setSelectedStats] = useState<SelectedRowStats | null>(null);
  const [matchList, setMatchList] = useState<RankedMatchSummary[] | null>(null);
  const [matchListLoading, setMatchListLoading] = useState(false);
  const [selectedMatch, setSelectedMatch] = useState<RankedMatchSummary | null>(null);

  const onSelectRow = useCallback(
    (project: SelectedProject, stats: SelectedRowStats) => {
      setSelectedProject(project);
      setSelectedStats(stats);
    },
    [],
  );

  // Load modes + groups once.
  useEffect(() => {
    let cancelled = false;
    Promise.all([api.getModes(), api.getModeGroups()])
      .then(([ms, gs]) => {
        if (cancelled) return;
        setModes(ms);
        setGroups(gs);
      })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, []);

  // Build the tab list: Overall, then groups (sorted), then ungrouped modes.
  // Within each multi-mode group the sub-tabs are: "All maps" (the group view)
  // and each member mode.
  const { topTabs, subTabsByGroup, modesByGroup } = useMemo(() => {
    const modesByGroup = new Map<string, Mode[]>();
    const ungroupedModes: Mode[] = [];
    for (const m of modes ?? []) {
      if (!m.enabled) continue;
      if (m.group_slug) {
        const arr = modesByGroup.get(m.group_slug) ?? [];
        arr.push(m);
        modesByGroup.set(m.group_slug, arr);
      } else {
        ungroupedModes.push(m);
      }
    }
    const orderedGroups = (groups ?? [])
      .slice()
      .sort((a, b) => a.sort_order - b.sort_order || a.slug.localeCompare(b.slug))
      .filter((g) => (modesByGroup.get(g.slug)?.length ?? 0) > 0);

    const topTabs: Tab[] = [
      OVERALL_TAB,
      ...orderedGroups.map<Tab>((g) => ({ kind: "group", slug: g.slug })),
      ...ungroupedModes.map<Tab>((m) => ({ kind: "mode", slug: m.slug })),
    ];

    const subTabsByGroup = new Map<string, Tab[]>();
    for (const g of orderedGroups) {
      const ms = modesByGroup.get(g.slug) ?? [];
      if (ms.length <= 1) continue;
      subTabsByGroup.set(g.slug, [
        { kind: "group", slug: g.slug },
        ...ms.map<Tab>((m) => ({ kind: "mode", slug: m.slug })),
      ]);
    }
    return { topTabs, subTabsByGroup, modesByGroup };
  }, [modes, groups]);

  // Active *top-level* tab: when a sub-tab (mode inside a group) is selected,
  // the top tab is the group it belongs to.
  const activeTopTab: Tab = useMemo(() => {
    if (tab.kind === "mode") {
      const m = modes?.find((mm) => mm.slug === tab.slug);
      if (m?.group_slug && subTabsByGroup.has(m.group_slug)) {
        return { kind: "group", slug: m.group_slug };
      }
    }
    return tab;
  }, [tab, modes, subTabsByGroup]);

  // Load the current tab's data whenever it changes.
  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (tab.kind === "overall") {
      setOverall(null);
      api.getOverallLeaderboard(100)
        .then((data) => { if (!cancelled) setOverall(data); })
        .catch((e) => { if (!cancelled) setError(String(e)); });
    } else if (tab.kind === "group") {
      setGroupEntries(null);
      api.getGroupLeaderboard(tab.slug, 100)
        .then((data) => { if (!cancelled) setGroupEntries(data); })
        .catch((e) => { if (!cancelled) setError(String(e)); });
    } else {
      setModeEntries(null);
      api.getModeLeaderboard(tab.slug, 100)
        .then((data) => { if (!cancelled) setModeEntries(data); })
        .catch((e) => { if (!cancelled) setError(String(e)); });
    }
    return () => { cancelled = true; };
  }, [tabKey(tab)]); // eslint-disable-line react-hooks/exhaustive-deps

  const closeModal = useCallback(() => {
    setSelectedProject(null);
    setSelectedStats(null);
    setMatchList(null);
    setSelectedMatch(null);
  }, []);

  // When a project is selected, load its match history. Scope to the active
  // mode/group when opened from a per-mode or per-group tab.
  useEffect(() => {
    if (!selectedProject) return;
    let cancelled = false;
    setMatchList(null);
    setSelectedMatch(null);
    setMatchListLoading(true);
    let modeIds: number[] | undefined;
    if (tab.kind === "mode") {
      const id = modes?.find((m) => m.slug === tab.slug)?.id;
      modeIds = id != null ? [id] : undefined;
    } else if (tab.kind === "group") {
      modeIds = (modesByGroup.get(tab.slug) ?? []).map((m) => m.id);
    }
    api.listProjectRankedMatches(selectedProject.id, { modeIds, limit: 50 })
      .then((data) => { if (!cancelled) { setMatchList(data); setMatchListLoading(false); } })
      .catch(() => { if (!cancelled) { setMatchList([]); setMatchListLoading(false); } });
    return () => { cancelled = true; };
  }, [selectedProject?.id, tabKey(tab)]); // eslint-disable-line react-hooks/exhaustive-deps

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

  const activeMode = tab.kind === "mode"
    ? modes?.find((m) => m.slug === tab.slug) ?? null
    : null;
  const activeGroup = tab.kind === "group"
    ? groups?.find((g) => g.slug === tab.slug) ?? null
    : null;
  const activeGroupModes = activeGroup
    ? modesByGroup.get(activeGroup.slug) ?? []
    : null;
  const activeTopSubTabs = activeTopTab.kind === "group"
    ? subTabsByGroup.get(activeTopTab.slug) ?? null
    : null;

  return (
    <div className="page-pad">
      <div className="lead">
        <h1>Leaderboard</h1>
        <p className="sub">The best snakes in the arena.</p>

        {modes && groups && (
          <div className="lb-tabs">
            {topTabs.map((t) => (
              <TabButton
                key={tabKey(t)}
                tab={t}
                active={tabKey(t) === tabKey(activeTopTab)}
                onClick={() => setTab(t)}
                modes={modes}
                groups={groups}
                modesByGroup={modesByGroup}
              />
            ))}
          </div>
        )}

        {activeTopSubTabs && (
          <div className="lb-subtabs">
            {activeTopSubTabs.map((t) => (
              <SubTabButton
                key={tabKey(t)}
                tab={t}
                active={tabKey(t) === tabKey(tab)}
                onClick={() => setTab(t)}
                modes={modes ?? []}
              />
            ))}
          </div>
        )}

        {error && (
          <div style={{ color: "var(--red)", marginBottom: 24, fontSize: 13 }}>
            Failed to load: {error}
          </div>
        )}

        {tab.kind === "overall" && (
          <OverallTable
            entries={overall}
            error={error}
            onSelectRow={onSelectRow}
          />
        )}

        {tab.kind === "group" && (
          <GroupTable
            entries={groupEntries}
            error={error}
            group={activeGroup}
            memberModes={activeGroupModes ?? []}
            onSelectRow={onSelectRow}
          />
        )}

        {tab.kind === "mode" && (
          <ModeTable
            entries={modeEntries}
            error={error}
            mode={activeMode}
            onSelectRow={onSelectRow}
          />
        )}
      </div>

      {selectedProject && (
        <MatchModal
          project={selectedProject}
          stats={selectedStats}
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

// ── Tab buttons ──────────────────────────────────────────────────────────────

function TabButton({
  tab,
  active,
  onClick,
  modes,
  groups,
  modesByGroup,
}: {
  tab: Tab;
  active: boolean;
  onClick: () => void;
  modes: Mode[];
  groups: ModeGroup[];
  modesByGroup: Map<string, Mode[]>;
}) {
  if (tab.kind === "overall") {
    return (
      <button className={`lb-tab${active ? " active" : ""}`} onClick={onClick}>
        Overall
      </button>
    );
  }
  if (tab.kind === "group") {
    const g = groups.find((x) => x.slug === tab.slug);
    const count = modesByGroup.get(tab.slug)?.length ?? 0;
    return (
      <button
        className={`lb-tab${active ? " active" : ""}`}
        onClick={onClick}
        title={g?.description ?? undefined}
      >
        {g?.name ?? tab.slug}
        {count > 1 && <span className="lb-tab-sub">{count} maps</span>}
      </button>
    );
  }
  const m = modes.find((x) => x.slug === tab.slug);
  return (
    <button
      className={`lb-tab${active ? " active" : ""}`}
      onClick={onClick}
      title={m?.description ?? undefined}
    >
      {m?.name ?? tab.slug}
      {m && <span className="lb-tab-sub">{m.participant_count}p</span>}
    </button>
  );
}

function SubTabButton({
  tab,
  active,
  onClick,
  modes,
}: {
  tab: Tab;
  active: boolean;
  onClick: () => void;
  modes: Mode[];
}) {
  if (tab.kind === "group") {
    return (
      <button className={`lb-subtab${active ? " active" : ""}`} onClick={onClick}>
        All maps
      </button>
    );
  }
  if (tab.kind === "mode") {
    const m = modes.find((x) => x.slug === tab.slug);
    return (
      <button
        className={`lb-subtab${active ? " active" : ""}`}
        onClick={onClick}
        title={m?.description ?? undefined}
      >
        {m?.name ?? tab.slug}
      </button>
    );
  }
  return null;
}

// ── Overall table ────────────────────────────────────────────────────────────

interface OverallTableProps {
  entries: OverallLeaderboardEntry[] | null;
  error: string | null;
  onSelectRow: (project: SelectedProject, stats: SelectedRowStats) => void;
}

function OverallTable({ entries, error, onSelectRow }: OverallTableProps) {
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
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr
              key={e.project_id}
              className={`lb-row lb-row-clickable ${e.rank <= 3 ? "lb-row-top" : ""}`}
              onClick={() =>
                onSelectRow(
                  {
                    id: e.project_id,
                    name: e.project_name,
                    language: e.language,
                    user_display_name: e.user_display_name,
                  },
                  {
                    kind: "overall",
                    overall_score: e.overall_score,
                    total_matches: e.total_matches,
                    modes_played: e.modes_played,
                  },
                )
              }
            >
              <td className="lb-td lb-rank">
                <span className="lb-rank-num">{e.rank}</span>
                <RankMedal rank={e.rank} />
              </td>
              <td className="lb-td lb-agent">
                <div className="lb-agent-inner">
                  <span className="lb-name">{e.project_name}</span>
                  <LangBadge lang={e.language} />
                </div>
              </td>
              <td className="lb-td lb-author">{e.user_display_name}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="lb-footnote">
        Overall averages each mode's per-mode score (0..1) within each group,
        then averages across groups — so a multi-map group counts the same as
        a single-mode tab. Eligible only once you've qualified in every enabled
        mode (qualified = played enough matches that the per-mode score is no
        longer NULL). Click a row to view the agent's stats and match replays.
      </p>
    </>
  );
}

// ── Per-group table ──────────────────────────────────────────────────────────

interface GroupTableProps {
  entries: GroupLeaderboardEntry[] | null;
  error: string | null;
  group: ModeGroup | null;
  memberModes: Mode[];
  onSelectRow: (project: SelectedProject, stats: SelectedRowStats) => void;
}

function GroupTable({ entries, error, group, memberModes, onSelectRow }: GroupTableProps) {
  if (entries === null && !error) {
    return <div style={{ color: "var(--text-faint)", fontSize: 13 }}>Loading…</div>;
  }
  if (entries !== null && entries.length === 0) {
    return (
      <div className="placeholder-card">
        <div className="big">no matches yet</div>
        <div>The scheduler hasn't run scored matches in this group yet.</div>
      </div>
    );
  }
  if (entries === null) return null;

  return (
    <>
      {group?.description && (
        <p className="lb-mode-desc">
          {group.description}{" "}
          <span className="muted">
            ({memberModes.length} {memberModes.length === 1 ? "mode" : "modes"})
          </span>
        </p>
      )}
      <table className="lb-table">
        <thead>
          <tr>
            <th className="lb-th lb-rank">#</th>
            <th className="lb-th lb-agent">Agent</th>
            <th className="lb-th lb-author">Author</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr
              key={e.project_id}
              className={`lb-row lb-row-clickable ${e.rank <= 3 ? "lb-row-top" : ""}`}
              onClick={() =>
                onSelectRow(
                  {
                    id: e.project_id,
                    name: e.project_name,
                    language: e.language,
                    user_display_name: e.user_display_name,
                  },
                  {
                    kind: "group",
                    group_score: e.group_score,
                    matches_played: e.matches_played,
                    modes_played: e.modes_played,
                    mode_count: memberModes.length,
                  },
                )
              }
            >
              <td className="lb-td lb-rank">
                <span className="lb-rank-num">{e.rank}</span>
                <RankMedal rank={e.rank} />
              </td>
              <td className="lb-td lb-agent">
                <div className="lb-agent-inner">
                  <span className="lb-name">{e.project_name}</span>
                  <LangBadge lang={e.language} />
                </div>
              </td>
              <td className="lb-td lb-author">{e.user_display_name}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="lb-footnote">
        Group score is the mean of the agent's per-mode scores (0..1) across
        the qualified modes in this group. Pick a sub-tab above to drill into
        a single map's leaderboard. Click a row to view the agent's stats and
        match replays.
      </p>
    </>
  );
}

// ── Per-mode table ───────────────────────────────────────────────────────────

interface ModeTableProps {
  entries: LeaderboardEntry[] | null;
  error: string | null;
  mode: Mode | null;
  onSelectRow: (project: SelectedProject, stats: SelectedRowStats) => void;
}

function ModeTable({ entries, error, mode, onSelectRow }: ModeTableProps) {
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
            (target {mode.target_matches_per_version} matches per version, {mode.avg_budget_ms.toFixed(0)} ms avg CPU budget)
          </span>
        </p>
      )}
      <table className="lb-table">
        <thead>
          <tr>
            <th className="lb-th lb-rank">#</th>
            <th className="lb-th lb-agent">Agent</th>
            <th className="lb-th" style={{ textAlign: "right", width: 64 }}>Score</th>
            <th className="lb-th">Categories</th>
            <th className="lb-th lb-author">Author</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr
              key={e.project_id}
              className={`lb-row lb-row-clickable ${e.rank <= 3 ? "lb-row-top" : ""}`}
              onClick={() =>
                onSelectRow(
                  {
                    id: e.project_id,
                    name: e.project_name,
                    language: e.language,
                    user_display_name: e.user_display_name,
                  },
                  {
                    kind: "mode",
                    score: e.score,
                    category_breakdown: e.category_breakdown,
                    matches_played: e.matches_played,
                  },
                )
              }
            >
              <td className="lb-td lb-rank">
                <span className="lb-rank-num">{e.rank}</span>
                <RankMedal rank={e.rank} />
              </td>
              <td className="lb-td lb-agent">
                <div className="lb-agent-inner">
                  <span className="lb-name">{e.project_name}</span>
                  <LangBadge lang={e.language} />
                </div>
              </td>
              <td className="lb-td" style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                {e.score.toFixed(3)}
              </td>
              <td className="lb-td">
                <CategoryBreakdown breakdown={e.category_breakdown} compact />
              </td>
              <td className="lb-td lb-author">{e.user_display_name}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="lb-footnote">
        Score is 0..1 (higher = better). Each category chip shows the fraction
        of the population the agent beats in that category, with ties as
        half-wins; the chip's bar fills proportionally. Multi modes rank
        within each match (population = participants of that match); solo
        modes rank across agents (population = every agent's mean per
        category). The score is the mean of those per-category
        scores. Click a row to view this agent's stats and match replays.
      </p>
    </>
  );
}

// Per-category chips. Each chip's bar fills 0..1 by `rank` (the fraction of
// the population this agent beats in that category — ties counted as half).
// The number on the chip is that same fraction; the tooltip carries the raw
// mean for context. The aggregate leaderboard score is the mean of these
// per-category fractions, so the strip explains the score directly.
function CategoryBreakdown({
  breakdown,
  compact = false,
}: {
  breakdown: Record<string, unknown>;
  compact?: boolean;
}) {
  const entries = Object.entries(breakdown ?? {});
  if (entries.length === 0) return <span className="muted">—</span>;
  const fontSize = compact ? 10 : 11;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, fontSize }}>
      {entries.map(([name, value]) => {
        const v = value as { raw?: number; rank?: number };
        const frac = v.rank;
        const fillPct = frac !== undefined ? Math.round(frac * 100) : null;
        return (
          <span
            key={name}
            style={{
              position: "relative",
              padding: "1px 6px",
              borderRadius: 3,
              border: "1px solid var(--text-faint)",
              overflow: "hidden",
              minWidth: 0,
            }}
            title={
              v.rank !== undefined
                ? `${name}: beats ${(v.rank * 100).toFixed(1)}% of the population · mean raw ${v.raw?.toFixed(3)}`
                : `${name}: mean ${v.raw?.toFixed(3)}`
            }
          >
            {fillPct !== null && (
              <span
                aria-hidden
                style={{
                  position: "absolute",
                  inset: 0,
                  width: `${fillPct}%`,
                  background: "var(--accent)",
                  opacity: 0.15,
                  pointerEvents: "none",
                }}
              />
            )}
            <span style={{ position: "relative", color: "var(--text-faint)" }}>
              {name.replace(/_/g, " ")}:
            </span>{" "}
            <span style={{ position: "relative" }}>
              {frac !== undefined ? frac.toFixed(2) : v.raw?.toFixed(3) ?? "—"}
            </span>
          </span>
        );
      })}
    </div>
  );
}

// ── Match modal ──────────────────────────────────────────────────────────────

interface MatchModalProps {
  project: SelectedProject;
  stats: SelectedRowStats | null;
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
  stats,
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

        {stats && <RowStatsHeader stats={stats} />}

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
              return (
                <MatchListRow
                  key={m.id}
                  match={m}
                  me={me ?? null}
                  opponents={others}
                  modeLabel={modeName(m.mode_id)}
                  active={selectedMatch?.id === m.id}
                  onClick={() => onSelectMatch(m)}
                />
              );
            })}
          </div>

          <div className="lb-match-player">
            {selectedMatch ? (
              <BundleSimPlayer
                key={selectedMatch.id}
                bundleKey={selectedMatch.id}
                getBundleUrl={getBundleUrl}
                participantNames={selectedMatch.participants
                  .slice()
                  .sort((a, b) => a.seat - b.seat)
                  .map((p) => p.project_name)}
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

function RowStatsHeader({ stats }: { stats: SelectedRowStats }) {
  return (
    <div className="lb-stats-header">
      <div className="lb-stats-row">
        {stats.kind === "overall" && (
          <>
            <StatChip label="Overall" value={fmt(stats.overall_score, 3)} highlight />
            <StatChip label="Matches" value={String(stats.total_matches)} />
            <StatChip label="Modes" value={String(stats.modes_played)} />
          </>
        )}
        {stats.kind === "group" && (
          <>
            <StatChip label="Score" value={fmt(stats.group_score, 3)} highlight />
            <StatChip label="Matches" value={String(stats.matches_played)} />
            <StatChip label="Modes" value={`${stats.modes_played}/${stats.mode_count}`} />
          </>
        )}
        {stats.kind === "mode" && (
          <>
            <StatChip label="Score" value={fmt(stats.score, 3)} highlight />
            <StatChip label="Matches" value={String(stats.matches_played)} />
          </>
        )}
      </div>
      {stats.kind === "mode" && Object.keys(stats.category_breakdown ?? {}).length > 0 && (
        <div className="lb-stats-breakdown">
          <CategoryBreakdown breakdown={stats.category_breakdown} />
        </div>
      )}
    </div>
  );
}

function StatChip({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div className="lb-stat-chip">
      <span className="lb-stat-chip-label">{label}</span>
      <span
        className="lb-stat-chip-value"
        style={highlight ? { color: "var(--accent)" } : undefined}
      >
        {value}
      </span>
    </div>
  );
}

interface MatchListRowProps {
  match: RankedMatchSummary;
  me: RankedMatchParticipant | null;
  opponents: string[];
  modeLabel: string;
  active: boolean;
  onClick: () => void;
}

function MatchListRow({ match, me, opponents, modeLabel, active, onClick }: MatchListRowProps) {
  const total = match.participants.length;
  const oStr  = opponents.length > 0
    ? `vs ${opponents.slice(0, 2).join(", ")}${opponents.length > 2 ? ` +${opponents.length - 2}` : ""}`
    : "";
  return (
    <div className={`lb-match-item${active ? " active" : ""}`} onClick={onClick} role="button">
      <div className="lb-match-item-top">
        <span className="lb-match-date">{formatDate(match.started_at)}</span>
        <SurvivalBadge rank={me?.survival_rank ?? null} total={total} />
      </div>
      <div className="lb-match-opponents">
        <span className="lb-match-mode">{modeLabel}</span>
        {oStr && <span style={{ marginLeft: 6 }}>· {oStr}</span>}
        <span className="lb-match-id" style={{ marginLeft: 6, color: "var(--text-faint)" }}>
          · #{match.id}
        </span>
      </div>
    </div>
  );
}
