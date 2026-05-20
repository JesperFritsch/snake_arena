// Mirrors the wire shapes in services/api/api/schemas.py and the sa_common
// dataclasses FastAPI serialises directly. Keep in sync with the backend.

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
export type BuildStatus = "building" | "ready" | "failed";

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

export interface BuildJob {
  id: number;
  project_id: number;
  status: JobStatus | string;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface BuildEnqueueResult {
  build_job_id: number;
  job: BuildJob | null;
}

export interface SubmitResult {
  submitted_version: number;
}

export interface UserOut {
  id: number;
  email: string;
  display_name: string;
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
  replay_r2_key: string | null;
  error: string | null;
  participants: ParticipantOut[];
}
