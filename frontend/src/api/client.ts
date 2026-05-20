import { useAuth } from "@clerk/clerk-react";
import { useMemo } from "react";
import type {
  BuildEnqueueResult,
  BuildJob,
  ProjectCreate,
  ProjectFiles,
  ProjectMeta,
  SubmitResult,
  UserOut,
} from "./types";

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

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
  getLanguages(): Promise<string[]>;
  me(): Promise<UserOut>;
  listProjects(): Promise<ProjectMeta[]>;
  createProject(body: ProjectCreate): Promise<ProjectMeta>;
  getProject(id: number): Promise<ProjectMeta>;
  getFiles(id: number): Promise<ProjectFiles>;
  saveFiles(id: number, files: ProjectFiles): Promise<ProjectMeta>;
  build(id: number): Promise<BuildEnqueueResult>;
  getBuildJob(id: number): Promise<BuildJob>;
  submit(id: number): Promise<SubmitResult>;
  deleteProject(id: number): Promise<void>;
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
      getProject: (id) => request(g, "GET", `/projects/${id}`),
      getFiles: (id) => request(g, "GET", `/projects/${id}/files`),
      saveFiles: (id, files) => request(g, "PUT", `/projects/${id}/files`, files),
      build: (id) => request(g, "POST", `/projects/${id}/build`),
      getBuildJob: (id) => request(g, "GET", `/build-jobs/${id}`),
      submit: (id) => request(g, "POST", `/projects/${id}/submit`),
      deleteProject: (id) => request(g, "DELETE", `/projects/${id}`),
    };
  }, [getToken]);
}
