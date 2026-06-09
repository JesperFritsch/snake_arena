import { useAuth } from "@clerk/clerk-react";
import { useMemo } from "react";
import type {
  GroupLeaderboardEntry,
  GuestSession,
  LanguageInfo,
  LeaderboardEntry,
  MapInfo,
  Mode,
  ModeGroup,
  OverallLeaderboardEntry,
  ProjectCreate,
  ProjectFiles,
  ProjectMeta,
  PublicProjectSummary,
  QuotaErrorDetail,
  QuotaStatus,
  RankedMatchSummary,
  SubmitQuotaStatus,
  SubmitResult,
  TestMatchCreate,
  TestMatchJob,
  UserOut,
} from "./types";

const GUEST_SESSION_KEY = "gridsnake_guest_session_id";

export function getGuestSessionId(): string {
  let id = localStorage.getItem(GUEST_SESSION_KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(GUEST_SESSION_KEY, id);
  }
  return id;
}

export function clearGuestSessionId(): void {
  localStorage.removeItem(GUEST_SESSION_KEY);
}

export const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    public body?: unknown,
    public retryAfter?: number,
  ) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
  }

  /** When the server raised a 429 with a quota-error body, return its parsed
   * shape so the caller can refresh its local quota display. */
  quotaDetail(): QuotaErrorDetail | null {
    const d = (this.body as { detail?: unknown } | undefined)?.detail;
    if (
      d && typeof d === "object" &&
      (d as { error?: string }).error === "quota_exceeded"
    ) {
      return d as QuotaErrorDetail;
    }
    return null;
  }
}

type TokenGetter = () => Promise<string | null>;

/**
 * Low-level request. Sends the Clerk JWT (when signed in) and always includes
 * X-Guest-Session so the backend can migrate guest data on sign-in.
 */
async function request<T>(
  getToken: TokenGetter,
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const token = await getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  headers["X-Guest-Session"] = getGuestSessionId();
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    throw await buildApiError(res);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Build a structured ApiError from a non-OK response. Pulls the JSON body
 * (when present) plus the Retry-After header so callers handling 429 quota
 * errors can refresh their local quota state without a second round-trip. */
async function buildApiError(res: Response): Promise<ApiError> {
  let detail = res.statusText;
  let body: unknown = undefined;
  try {
    body = await res.json();
    const d = (body as { detail?: unknown })?.detail;
    if (typeof d === "string") {
      detail = d;
    } else if (d && typeof d === "object" && typeof (d as { message?: unknown }).message === "string") {
      detail = (d as { message: string }).message;
    } else if (d !== undefined) {
      detail = JSON.stringify(d);
    }
  } catch {
    /* non-JSON error body; keep statusText */
  }
  const ra = res.headers.get("Retry-After");
  const retryAfter = ra ? Number(ra) : undefined;
  return new ApiError(res.status, detail, body, Number.isFinite(retryAfter) ? retryAfter : undefined);
}

export interface ApiClient {
  getGuestSession(): Promise<GuestSession>;
  claimGuestSession(sessionId: string): Promise<{ migrated: number }>;
  getLanguages(): Promise<LanguageInfo[]>;
  me(): Promise<UserOut>;
  getModes(): Promise<Mode[]>;
  getModeGroups(): Promise<ModeGroup[]>;
  getModeLeaderboard(modeSlug: string, limit?: number): Promise<LeaderboardEntry[]>;
  getGroupLeaderboard(groupSlug: string, limit?: number): Promise<GroupLeaderboardEntry[]>;
  getOverallLeaderboard(limit?: number): Promise<OverallLeaderboardEntry[]>;
  listProjects(): Promise<ProjectMeta[]>;
  createProject(body: ProjectCreate): Promise<ProjectMeta>;
  checkNameAvailable(name: string): Promise<{ available: boolean; reason?: string }>;
  getProject(id: number): Promise<ProjectMeta>;
  getFiles(id: number): Promise<ProjectFiles>;
  saveFiles(id: number, files: ProjectFiles): Promise<ProjectMeta>;
  uploadProjectImage(projectId: number, file: File, onProgress?: (pct: number) => void): Promise<ProjectMeta>;
  downloadProto(): Promise<void>;
  downloadHarness(language: string): Promise<void>;
  submit(id: number): Promise<SubmitResult>;
  deleteProject(id: number): Promise<void>;
  getSubmittedFiles(id: number): Promise<ProjectFiles>;
  restoreFromSubmitted(id: number): Promise<ProjectMeta>;
  listOpponents(): Promise<PublicProjectSummary[]>;
  enqueueTestMatch(body: TestMatchCreate): Promise<TestMatchJob>;
  listTestMatchJobs(projectId: number, limit?: number): Promise<TestMatchJob[]>;
  pinTestMatch(jobId: number, pinned: boolean): Promise<TestMatchJob>;
  cancelTestMatch(jobId: number): Promise<void>;
  getTestMatchBundleUrl(jobId: number): Promise<{ url: string }>;
  listProjectRankedMatches(projectId: number, opts?: { modeIds?: number[]; limit?: number }): Promise<RankedMatchSummary[]>;
  getMatchBundleUrl(matchId: number): Promise<{ url: string }>;
  getTestMatchQuota(): Promise<QuotaStatus>;
  getSubmitQuota(): Promise<SubmitQuotaStatus>;
  getUploadImageQuota(): Promise<QuotaStatus>;
  listMaps(): Promise<MapInfo[]>;
}

async function downloadBlob(getToken: TokenGetter, path: string, filename: string): Promise<void> {
  const token = await getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  headers["X-Guest-Session"] = getGuestSessionId();
  const res = await fetch(`${BASE_URL}${path}`, { headers });
  if (!res.ok) throw await buildApiError(res);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const _CHUNK_SIZE = 90 * 1024 * 1024; // 90 MB — stays under Cloudflare's 100 MB per-request limit

async function uploadProjectImageChunked(
  getToken: TokenGetter,
  projectId: number,
  file: File,
  onProgress?: (pct: number) => void,
): Promise<ProjectMeta> {
  const totalChunks = Math.max(1, Math.ceil(file.size / _CHUNK_SIZE));

  const authHeaders = async (): Promise<Record<string, string>> => {
    const token = await getToken();
    const h: Record<string, string> = { "X-Guest-Session": getGuestSessionId() };
    if (token) h["Authorization"] = `Bearer ${token}`;
    return h;
  };

  // 1. Start session
  const startRes = await fetch(`${BASE_URL}/projects/${projectId}/upload-image/start`, {
    method: "POST",
    headers: { ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify({ total_chunks: totalChunks }),
  });
  if (!startRes.ok) throw await buildApiError(startRes);
  const { upload_id } = (await startRes.json()) as { upload_id: string };

  // 2. Upload chunks sequentially
  for (let i = 0; i < totalChunks; i++) {
    const chunk = file.slice(i * _CHUNK_SIZE, (i + 1) * _CHUNK_SIZE);
    const form = new FormData();
    form.append("file", chunk);
    const chunkRes = await fetch(
      `${BASE_URL}/projects/${projectId}/upload-image/chunk?upload_id=${upload_id}&index=${i}`,
      { method: "POST", headers: await authHeaders(), body: form },
    );
    if (!chunkRes.ok) throw await buildApiError(chunkRes);
    // totalChunks + 1 steps total (finalize counts as the last step)
    onProgress?.(((i + 1) / (totalChunks + 1)) * 100);
  }

  // 3. Finalize — assembles chunks and loads the Docker image
  const finalRes = await fetch(
    `${BASE_URL}/projects/${projectId}/upload-image/finalize?upload_id=${upload_id}`,
    { method: "POST", headers: await authHeaders() },
  );
  if (!finalRes.ok) throw await buildApiError(finalRes);
  onProgress?.(100);
  return (await finalRes.json()) as ProjectMeta;
}

/** React hook returning an API client bound to the current Clerk session. */
export function useApi(): ApiClient {
  const { getToken } = useAuth();

  return useMemo<ApiClient>(() => {
    const g: TokenGetter = () => getToken();
    return {
      getGuestSession: () => request(g, "POST", "/guest/session"),
      claimGuestSession: (sessionId) => request(g, "POST", "/guest/claim", { session_id: sessionId }),
      getLanguages: () => request(g, "GET", "/languages"),
      me: () => request(g, "GET", "/me"),
      getModes: () => request(g, "GET", "/modes"),
      getModeGroups: () => request(g, "GET", "/mode-groups"),
      getModeLeaderboard: (modeSlug, limit = 100) =>
        request(g, "GET", `/leaderboard?mode=${encodeURIComponent(modeSlug)}&limit=${limit}`),
      getGroupLeaderboard: (groupSlug, limit = 100) =>
        request(g, "GET", `/leaderboard/group?group=${encodeURIComponent(groupSlug)}&limit=${limit}`),
      getOverallLeaderboard: (limit = 100) =>
        request(g, "GET", `/leaderboard/overall?limit=${limit}`),
      listProjects: () => request(g, "GET", "/projects"),
      createProject: (body) => request(g, "POST", "/projects", body),
      checkNameAvailable: (name) =>
        request(g, "GET", `/projects/name-available?name=${encodeURIComponent(name)}`),
      getProject: (id) => request(g, "GET", `/projects/${id}`),
      getFiles: (id) => request(g, "GET", `/projects/${id}/files`),
      saveFiles: (id, files) => request(g, "PUT", `/projects/${id}/files`, files),
      uploadProjectImage: (projectId, file, onProgress) =>
        uploadProjectImageChunked(g, projectId, file, onProgress),
      downloadProto: () => downloadBlob(g, "/download/proto", "sim_interface.proto"),
      downloadHarness: (language) =>
        downloadBlob(g, `/download/harness/${language}`, `snake-harness-${language}.zip`),
      submit: (id) => request(g, "POST", `/projects/${id}/submit`),
      deleteProject: (id) => request(g, "DELETE", `/projects/${id}`),
      getSubmittedFiles: (id) => request(g, "GET", `/projects/${id}/files/submitted`),
      restoreFromSubmitted: (id) => request(g, "POST", `/projects/${id}/restore`),
      listOpponents: () => request(g, "GET", "/test-matches/opponents"),
      enqueueTestMatch: (body) => request(g, "POST", "/test-matches", body),
      listTestMatchJobs: (projectId, limit = 10) =>
        request(g, "GET", `/test-matches?player_project_id=${projectId}&limit=${limit}`),
      pinTestMatch: (jobId, pinned) =>
        request(g, "PATCH", `/test-matches/${jobId}/pin`, { pinned }),
      cancelTestMatch: (jobId) =>
        request(g, "POST", `/test-matches/${jobId}/cancel`),
      getTestMatchBundleUrl: (jobId) =>
        request(g, "GET", `/test-matches/${jobId}/bundle-url`),
      listProjectRankedMatches: (projectId, opts) => {
        const params = new URLSearchParams({
          project_id: String(projectId),
          limit: String(opts?.limit ?? 20),
        });
        for (const id of opts?.modeIds ?? []) params.append("mode_ids", String(id));
        return request(g, "GET", `/matches/for-project?${params.toString()}`);
      },
      getMatchBundleUrl: (matchId) =>
        request(g, "GET", `/matches/${matchId}/bundle-url`),
      getTestMatchQuota: () => request(g, "GET", "/test-matches/quota"),
      getSubmitQuota: () => request(g, "GET", "/projects/submit-quota"),
      getUploadImageQuota: () => request(g, "GET", "/projects/upload-image-quota"),
      listMaps: () => request(g, "GET", "/maps"),
    };
  }, [getToken]);
}
