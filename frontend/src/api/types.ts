// Mirrors the wire shapes in services/api/api/schemas.py and the sa_common
// dataclasses FastAPI serialises directly. Keep in sync with the backend.

export interface LanguageInfo {
  name: string;
  version: string | null;
}

export type FileEncoding = "utf-8" | "base64";

export interface ProjectFile {
  path: string;
  content: string;
  encoding: FileEncoding;
}

export interface ProjectFiles {
  files: ProjectFile[];
}

export type ProjectSource = "browser" | "external_image";
export type BuildStatus = "saved" | "building" | "ready" | "failed";

// ProjectMeta (projects.py) — no code archives.
export interface ProjectMeta {
  id: number;
  user_id: number;
  name: string;
  language: string;
  source: ProjectSource | string;
  dev_image_tag: string | null;
  dev_build_status: BuildStatus | string | null;
  dev_built_at: string | null;
  submitted_image_tag: string | null;
  submitted_version: number;
  submitted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectCreate {
  name: string;
  language: string;
  source?: ProjectSource;
  files?: ProjectFile[];
}

export type JobStatus =
  | "queued"
  | "running"
  | "success"
  | "failure"
  | "cancelled";

export interface SubmitResult {
  submitted_version: number;
}

export interface UserOut {
  id: number;
  email: string;
  display_name: string;
}

export interface TestMatchJob {
  id: number;
  status: JobStatus | string;
  player_project_id: number;
  opponent_project_ids: number[];
  sim_args: Record<string, unknown>;
  requested_by: number | null;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  match_id: number | null;
  error: string | null;
  bundle_key: string | null;
  pinned: boolean;
  participant_names: string[];  // [player, opp1, opp2, ...] ordered by seat
  match_number: number | null;  // project-relative sequence number (1 = oldest)
}

export interface TestMatchCreate {
  player_project_id: number;
  opponent_project_ids: number[];
  sim_args: { food: number; grid_width?: number; grid_height?: number };
}

export interface PublicProjectSummary {
  id: number;
  name: string;
  language: string;
  submitted_version: number;
  submitted_at: string;
  user_display_name: string;
}

// ---- match shapes (used by the stubbed match viewer / leaderboard) --------

export interface ParticipantOut {
  seat: number;
  project_id: number;
  project_version: number;
  final_length: number | null;
  fatal_step: number | null;
  survival_rank: number | null;
  killed_by_budget: boolean;
  metrics: Record<string, unknown>;
}

export interface MatchDetail {
  id: number;
  match_uuid: string;
  status: string;
  mode: string;
  sim_args: Record<string, unknown>;
  started_at: string;
  finished_at: string | null;
  bundle_key: string | null;
  error: string | null;
  participants: ParticipantOut[];
}

export interface RankedMatchParticipant {
  seat: number;
  project_id: number;
  project_name: string;
  final_length: number | null;
  survival_rank: number | null;
  metrics: Record<string, unknown>;
}

export interface RankedMatchSummary {
  id: number;
  match_uuid: string;
  status: string;
  mode_id: number | null;
  started_at: string;
  finished_at: string | null;
  bundle_key: string | null;
  participants: RankedMatchParticipant[];
}

export interface LeaderboardEntry {
  rank: number;
  project_id: number;
  project_name: string;
  language: string;
  user_display_name: string;
  matches_played: number;
  avg_score: number;
  best_score: number;
  avg_rank: number;
  avg_length: number | null;
}

export interface OverallLeaderboardEntry {
  rank: number;
  project_id: number;
  project_name: string;
  language: string;
  user_display_name: string;
  overall_score: number;
  total_matches: number;
  modes_played: number;
}

export interface Mode {
  id: number;
  slug: string;
  name: string;
  description: string | null;
  participant_count: number;
  sim_args: Record<string, unknown>;
  map_slug: string | null;
  budget_ms: number;
  target_matches_per_version: number;
  enabled: boolean;
}
