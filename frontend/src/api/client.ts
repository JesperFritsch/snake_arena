import { useAuth } from "@clerk/clerk-react";
import { useMemo } from "react";
import type {
  LanguageInfo,
  ProjectCreate,
  ProjectFiles,
  ProjectMeta,
  PublicProjectSummary,
  SubmitResult,
  TestMatchCreate,
  TestMatchJob,
  UserOut,
} from "./types";

export const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
  }
}

type TokenGetter = () => Promise<string | null>;

/**
 * Low-level request. Pulls a fresh Clerk session token and sends it as a
 * Bearer header — the backend (api/auth.py) verifies it against Clerk's JWKS
 * and resolves it to a local users row. Because we customised the *default*
 * session token (email/name claims), getToken() needs no template argument.
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
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data?.detail === "string" ? data.detail : JSON.stringify(data);
    } catch {
      /* non-JSON error body; keep statusText */
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export interface ApiClient {
  getLanguages(): Promise<LanguageInfo[]>;
  me(): Promise<UserOut>;
  listProjects(): Promise<ProjectMeta[]>;
  createProject(body: ProjectCreate): Promise<ProjectMeta>;
  checkNameAvailable(name: string): Promise<{ available: boolean; reason?: string }>;
  getProject(id: number): Promise<ProjectMeta>;
  getFiles(id: number): Promise<ProjectFiles>;
  saveFiles(id: number, files: ProjectFiles): Promise<ProjectMeta>;
  uploadProjectImage(projectId: number, file: File): Promise<ProjectMeta>;
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
}

async function downloadBlob(getToken: TokenGetter, path: string, filename: string): Promise<void> {
  const token = await getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${BASE_URL}${path}`, { headers });
  if (!res.ok) {
    let detail = res.statusText;
    try { const d = await res.json(); detail = d?.detail ?? detail; } catch { /* */ }
    throw new ApiError(res.status, detail);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

async function uploadFile<T>(getToken: TokenGetter, path: string, file: File): Promise<T> {
  const token = await getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE_URL}${path}`, { method: "POST", headers, body: form });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data?.detail === "string" ? data.detail : JSON.stringify(data);
    } catch { /* non-JSON */ }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** React hook returning an API client bound to the current Clerk session. */
export function useApi(): ApiClient {
  const { getToken } = useAuth();

  return useMemo<ApiClient>(() => {
    const g: TokenGetter = () => getToken();
    return {
      getLanguages: () => request(g, "GET", "/languages"),
      me: () => request(g, "GET", "/me"),
      listProjects: () => request(g, "GET", "/projects"),
      createProject: (body) => request(g, "POST", "/projects", body),
      checkNameAvailable: (name) =>
        request(g, "GET", `/projects/name-available?name=${encodeURIComponent(name)}`),
      getProject: (id) => request(g, "GET", `/projects/${id}`),
      getFiles: (id) => request(g, "GET", `/projects/${id}/files`),
      saveFiles: (id, files) => request(g, "PUT", `/projects/${id}/files`, files),
      uploadProjectImage: (projectId, file) =>
        uploadFile<ProjectMeta>(g, `/projects/${projectId}/upload-image`, file),
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
    };
  }, [getToken]);
}
