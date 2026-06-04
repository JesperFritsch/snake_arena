import { useCallback, useEffect, useRef, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle, type ImperativePanelHandle } from "react-resizable-panels";
import { useApi, ApiError } from "../api/client";
import type { LanguageInfo, ProjectFile, ProjectMeta, ProjectSource, QuotaStatus, SubmitQuotaStatus, TestMatchJob } from "../api/types";
import { FileTree } from "../components/FileTree";
import { CodeEditor } from "../components/CodeEditor";
import { ImageUploadPanel } from "../components/ImageUploadPanel";
import { MatchViewer } from "../components/MatchViewer";
import { QuotaIndicator, SubmitQuotaIndicator } from "../components/QuotaIndicator";
import { TestDialog, loadTestSettings, saveTestSettings } from "../components/TestDialog";
import type { TestSettings } from "../components/TestDialog";
import { NewProjectDialog } from "../components/NewProjectDialog";
import { useIsMobile } from "../lib/useIsMobile";
import { fmtLang } from "../lib/editor";
import { useToast } from "../components/Toast";

const TERMINAL: ReadonlySet<string> = new Set(["success", "failure", "cancelled"]);

function serialize(files: ProjectFile[]): string {
  return JSON.stringify(
    [...files].sort((a, b) => a.path.localeCompare(b.path)).map((f) => [f.path, f.content]),
  );
}

type MobileTab = "files" | "editor" | "viewer";

export function EditorPage() {
  const api = useApi();
  const { push } = useToast();

  const [languages, setLanguages] = useState<LanguageInfo[]>([]);
  const [projects, setProjects] = useState<ProjectMeta[]>([]);
  const [meta, setMeta] = useState<ProjectMeta | null>(null);
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [originalSig, setOriginalSig] = useState<string>("[]");
  const [activePath, setActivePath] = useState<string | null>(null);

  const [viewMode, setViewMode] = useState<"dev" | "submitted">("dev");
  const [submittedFiles, setSubmittedFiles] = useState<ProjectFile[]>([]);
  const [submittedActivePath, setSubmittedActivePath] = useState<string | null>(null);

  const [matchTabs, setMatchTabs] = useState<TestMatchJob[]>([]);
  const [activeTabId, setActiveTabId] = useState<number | null>(null);
  const [testDialogOpen, setTestDialogOpen] = useState(false);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [busy, setBusy] = useState<"" | "save" | "submit" | "delete" | "restore">("");
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [mobileTab, setMobileTab] = useState<MobileTab>("editor");
  const [submitQuota, setSubmitQuota] = useState<SubmitQuotaStatus | null>(null);
  const [testQuota, setTestQuota] = useState<QuotaStatus | null>(null);
  const isMobile = useIsMobile();

  const refreshSubmitQuota = useCallback(() => {
    api.getSubmitQuota().then(setSubmitQuota).catch(() => {});
  }, [api]);

  const refreshTestQuota = useCallback(() => {
    api.getTestMatchQuota().then(setTestQuota).catch(() => {});
  }, [api]);

  useEffect(() => {
    refreshSubmitQuota();
    refreshTestQuota();
  }, [refreshSubmitQuota, refreshTestQuota]);

  const submitBlocked =
    submitQuota != null &&
    (submitQuota.hourly.remaining === 0 || submitQuota.daily.remaining === 0);
  const testBlocked = testQuota != null && testQuota.remaining === 0;

  const viewerPanelRef = useRef<ImperativePanelHandle>(null);
  const shortcutSaveRef = useRef<() => Promise<boolean>>(() => Promise.resolve(false));
  const shortcutTestRef = useRef<() => Promise<void>>(() => Promise.resolve());

  const dirty = serialize(files) !== originalSig;
  const origFiles = JSON.parse(originalSig) as [string, string][];
  const dirtyPaths = new Set(
    files
      .filter((f) => {
        const match = origFiles.find(([p]) => p === f.path);
        return !match || match[1] !== f.content;
      })
      .map((f) => f.path),
  );

  // ---- load projects and languages on mount ------------------------------
  useEffect(() => {
    api.getLanguages().then(setLanguages).catch(() => {});
    api
      .listProjects()
      .then((ps) => {
        setProjects(ps);
        if (ps.length) void selectProject(ps[0].id, ps);
      })
      .catch((e) => push(`Failed to load projects: ${e.message}`, "error"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectProject = useCallback(
    async (id: number, knownList?: ProjectMeta[]) => {
      const list = knownList ?? projects;
      const m = list.find((p) => p.id === id) ?? (await api.getProject(id));
      setMeta(m);
      setMatchTabs([]);
      setActiveTabId(null);
      setViewMode("dev");
      setSubmittedFiles([]);
      setSubmittedActivePath(null);
      if (m.source === "external_image") {
        setFiles([]);
        setOriginalSig("[]");
        setActivePath(null);
        return;
      }
      setLoadingFiles(true);
      try {
        const { files: fetched } = await api.getFiles(id);
        setFiles(fetched);
        setOriginalSig(serialize(fetched));
        setActivePath(fetched[0]?.path ?? null);
      } catch (e) {
        // Brand-new browser projects may not have any files yet.
        setFiles([]);
        setOriginalSig("[]");
        setActivePath(null);
        if (e instanceof ApiError && e.status !== 404) {
          push(`Failed to load files: ${e.detail}`, "error");
        }
      } finally {
        setLoadingFiles(false);
      }
    },
    [api, projects, push],
  );

  // ---- editing ------------------------------------------------------------
  const updateActive = (content: string) => {
    if (!activePath) return;
    setFiles((fs) => fs.map((f) => (f.path === activePath ? { ...f, content } : f)));
  };

  // FileTree validates name uniqueness before calling these.
  const addFile = (path: string) => {
    setFiles((fs) => [...fs, { path, content: "", encoding: "utf-8" }]);
    setActivePath(path);
    setMobileTab("editor");
  };

  const renameFile = (oldPath: string, newPath: string) => {
    setFiles((fs) => fs.map((f) => (f.path === oldPath ? { ...f, path: newPath } : f)));
    if (activePath === oldPath) setActivePath(newPath);
  };

  const deleteFile = (path: string) => {
    if (!window.confirm(`Delete ${path}? (applies on next save)`)) return;
    setFiles((fs) => fs.filter((f) => f.path !== path));
    if (activePath === path) setActivePath(files.find((f) => f.path !== path)?.path ?? null);
  };

  // ---- save / build / submit ---------------------------------------------
  const save = useCallback(async (): Promise<boolean> => {
    if (!meta) return false;
    if (files.length === 0) {
      push("Browser projects need at least one file.", "error");
      return false;
    }
    setBusy("save");
    try {
      const refreshed = await api.saveFiles(meta.id, { files });
      setMeta(refreshed);
      setOriginalSig(serialize(files));
      push("Saved.");
      return true;
    } catch (e) {
      push(`Save failed: ${e instanceof ApiError ? e.detail : e}`, "error");
      return false;
    } finally {
      setBusy("");
    }
  }, [api, files, meta, push]);

  // Match + build status now arrive over the match WebSocket (no polling).
  const onMatchStatus = (jobId: number, status: string) => {
    setMatchTabs((prev) =>
      prev.map((t) => (t.id === jobId ? { ...t, status } : t)),
    );
    if (TERMINAL.has(status)) {
      push(
        status === "success" ? "Test match finished." : `Test match ${status}.`,
        status === "success" ? "info" : "error",
      );
    }
  };

  const onBuildStatus = (status: string) => {
    // Keep the toolbar build pill in sync with the live build event.
    setMeta((m) => (m ? { ...m, dev_build_status: status } : m));
  };

  const onTestMatchEnqueued = (job: TestMatchJob) => {
    setMatchTabs((prev) => {
      if (prev.some((t) => t.id === job.id)) return prev;
      // Recycle the active tab only when it shows the previous most-recent
      // match (match_number === new - 1). Reviewing an older run shouldn't
      // get clobbered — that opens a new tab instead.
      const active = prev.find((t) => t.id === activeTabId);
      const replaceActive =
        active &&
        TERMINAL.has(active.status) &&
        job.match_number != null &&
        active.match_number != null &&
        active.match_number === job.match_number - 1;
      if (replaceActive) {
        return prev.map((t) => (t.id === active!.id ? job : t));
      }
      return [...prev, job];
    });
    setActiveTabId(job.id);
    setMobileTab("viewer");
    push(`Test match ${job.match_number != null ? `#${job.match_number}` : `#${job.id}`} queued.`);
    viewerPanelRef.current?.resize(52);
  };

  const onJobPinChange = (job: TestMatchJob) => {
    setMatchTabs((prev) => prev.map((t) => (t.id === job.id ? job : t)));
  };

  const onJobsRefreshed = useCallback((freshJobs: TestMatchJob[]) => {
    const byId = new Map(freshJobs.map((j) => [j.id, j]));
    setMatchTabs((prev) => prev.map((t) => byId.get(t.id) ?? t));
  }, []);

  const onTabSelect = (id: number) => setActiveTabId(id);

  const onTabClose = (id: number) => {
    const next = matchTabs.filter((t) => t.id !== id);
    setMatchTabs(next);
    if (activeTabId === id) {
      setActiveTabId(next.length > 0 ? next[next.length - 1].id : null);
    }
  };

  const onOpenMatch = (job: TestMatchJob, newTab: boolean) => {
    if (!newTab && activeTabId !== null) {
      setMatchTabs((prev) =>
        prev.some((t) => t.id === job.id)
          ? prev
          : prev.map((t) => (t.id === activeTabId ? job : t)),
      );
    } else {
      setMatchTabs((prev) =>
        prev.some((t) => t.id === job.id) ? prev : [...prev, job],
      );
    }
    setActiveTabId(job.id);
  };

  const runTestMatch = useCallback(async (settings: TestSettings) => {
    if (!meta) return;
    const w = parseInt(settings.gridWidth);
    const h = parseInt(settings.gridHeight);
    const hasGrid = settings.gridWidth !== "" && settings.gridHeight !== "" && !isNaN(w) && !isNaN(h);
    const sim_args: { food: number; grid_width?: number; grid_height?: number } = { food: settings.food };
    if (hasGrid) { sim_args.grid_width = w; sim_args.grid_height = h; }
    try {
      const job = await api.enqueueTestMatch({
        player_project_id: meta.id,
        opponent_project_ids: settings.opponentIds,
        sim_args,
      });
      saveTestSettings(meta.id, settings);
      onTestMatchEnqueued(job);
    } catch (e) {
      push(`Could not start test match: ${e instanceof ApiError ? e.detail : e}`, "error");
      throw e;
    } finally {
      refreshTestQuota();
    }
  }, [api, meta, onTestMatchEnqueued, push, refreshTestQuota]);

  const handleTest = async () => {
    if (!meta) return;
    if (meta.source === "browser" && dirty && !(await save())) return;
    const saved = loadTestSettings(meta.id);
    if (!saved) {
      setTestDialogOpen(true);
      return;
    }
    try {
      await runTestMatch(saved);
    } catch (e) {
      // Toast already shown by runTestMatch. Only fall back into the settings
      // dialog when the failure is settings-related (stale opponent IDs after
      // a project was deleted / DB reset). Other errors — e.g. 409 because a
      // match is already queued — should not pop the dialog.
      const staleOpponent =
        e instanceof ApiError && e.status === 404 && /opponent/i.test(e.detail);
      if (staleOpponent) {
        saveTestSettings(meta.id, { ...saved, opponentIds: [] });
        setTestDialogOpen(true);
      }
    }
  };

  // Keep shortcut refs current so the single-mount listener always calls the
  // latest closures (which capture up-to-date state like `meta` and `dirty`).
  shortcutSaveRef.current = save;
  shortcutTestRef.current = handleTest;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (!mod) return;
      if (e.key === "s") {
        e.preventDefault();
        void shortcutSaveRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleTestSettings = async () => {
    if (!meta) return;
    if (meta.source === "browser" && dirty && !(await save())) return;
    setTestDialogOpen(true);
  };

  const submit = async () => {
    if (!meta) return;
    if (meta.source === "browser" && dirty && !(await save())) return;
    setBusy("submit");
    try {
      const res = await api.submit(meta.id);
      push(`Submitted as version ${res.submitted_version}.`);
      api.getProject(meta.id).then(setMeta).catch(() => {});
    } catch (e) {
      push(e instanceof ApiError ? e.detail : String(e), "error");
    } finally {
      // Refresh quota after every attempt — success consumed a slot;
      // a 429 means we hit the cap; other failures left the count untouched.
      refreshSubmitQuota();
      setBusy("");
    }
  };

  // Throws on failure so NewProjectDialog can surface it inline.
  const createProject = async (name: string, language: string, source: ProjectSource) => {
    const created = await api.createProject({ name, language, source });
    const next = [created, ...projects];
    setProjects(next);
    await selectProject(created.id, next);
    push(`Created “${name}”.`);
  };

  const deleteCurrentProject = async () => {
    if (!meta) return;
    if (!window.confirm(`Delete "${meta.name}"? This cannot be undone.`)) return;
    setBusy("delete");
    try {
      await api.deleteProject(meta.id);
      const next = projects.filter((p) => p.id !== meta.id);
      setProjects(next);
      if (next.length) {
        await selectProject(next[0].id, next);
      } else {
        setMeta(null);
        setFiles([]);
        setOriginalSig("[]");
        setActivePath(null);
      }
      push(`Deleted "${meta.name}".`);
    } catch (e) {
      push(`Could not delete project: ${e instanceof ApiError ? e.detail : e}`, "error");
    } finally {
      setBusy("");
    }
  };

  const viewSubmitted = async () => {
    if (!meta) return;
    setLoadingFiles(true);
    try {
      const { files: fetched } = await api.getSubmittedFiles(meta.id);
      setSubmittedFiles(fetched);
      setSubmittedActivePath(fetched[0]?.path ?? null);
      setViewMode("submitted");
    } catch (e) {
      push(`Could not load submitted version: ${e instanceof ApiError ? e.detail : e}`, "error");
    } finally {
      setLoadingFiles(false);
    }
  };

  const restoreCurrentProject = async () => {
    if (!meta) return;
    if (!window.confirm(`Restore dev code to submitted version v${meta.submitted_version}? Unsaved dev changes will be overwritten.`)) return;
    setBusy("restore");
    try {
      const refreshed = await api.restoreFromSubmitted(meta.id);
      setMeta(refreshed);
      const { files: fetched } = await api.getFiles(meta.id);
      setFiles(fetched);
      setOriginalSig(serialize(fetched));
      setActivePath(fetched[0]?.path ?? null);
      setViewMode("dev");
      push("Dev code restored to submitted version.");
    } catch (e) {
      push(`Restore failed: ${e instanceof ApiError ? e.detail : e}`, "error");
    } finally {
      setBusy("");
    }
  };

  // ---- toolbar ------------------------------------------------------------
  const toolbar = (
    <header className="panel-head editor-toolbar">
      <select
        className="select"
        value={meta?.id ?? ""}
        onChange={(e) => selectProject(Number(e.target.value))}
      >
        {projects.length === 0 && <option value="">No projects</option>}
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name} · {fmtLang(p.language, languages)}
          </option>
        ))}
      </select>
      <button className="btn ghost" onClick={() => setNewProjectOpen(true)}>
        + Project
      </button>
      <button
        className="btn ghost danger"
        disabled={!meta || busy === "delete"}
        onClick={deleteCurrentProject}
      >
        {busy === "delete" ? "Deleting…" : "Delete"}
      </button>

      {meta && (
        <span className={`pill ${meta.dev_build_status ?? ""}`}>
          <span className="dot" />
          {meta.dev_build_status ?? "no build"}
        </span>
      )}
      {meta && viewMode === "dev" && (
        <>
          <button
            className="btn ghost"
            disabled={busy !== "" || testBlocked}
            onClick={handleTest}
            title={testBlocked ? "Hourly test-match limit reached — see badge for reset time." : undefined}
          >
            Test
          </button>
          <button
            className="btn ghost"
            disabled={busy !== ""}
            onClick={handleTestSettings}
            title="Test settings"
            style={{ padding: "2px 7px" }}
          >
            ⚙
          </button>
          <QuotaIndicator status={testQuota} label="tests/hr" />
        </>
      )}

      {meta && meta.submitted_version > 0 && viewMode === "dev" && (
        <button className="btn ghost" disabled={loadingFiles} onClick={viewSubmitted}>
          v{meta.submitted_version} view submitted
        </button>
      )}
      {viewMode === "submitted" && (
        <button className="btn ghost" onClick={() => setViewMode("dev")}>
          ◂ back to dev
        </button>
      )}

      <span className="spacer" />

      {viewMode === "submitted" ? (
        <button
          className="btn"
          disabled={busy === "restore"}
          onClick={restoreCurrentProject}
        >
          {busy === "restore" ? "Restoring…" : "Restore to dev"}
        </button>
      ) : (
        <>
          {meta?.source === "browser" && (
            <button className="btn" disabled={!meta || !dirty || busy === "save"} onClick={save}>
              {busy === "save" ? "Saving…" : dirty ? "Save" : "Saved"}
            </button>
          )}
          <SubmitQuotaIndicator
            hourly={submitQuota?.hourly ?? null}
            daily={submitQuota?.daily ?? null}
          />
          <button
            className="btn primary"
            disabled={!meta || busy !== "" || submitBlocked}
            onClick={submit}
            title={submitBlocked ? "Submission limit reached — see the badge for reset time." : undefined}
          >
            {busy === "submit" ? "Submitting…" : "Submit"}
          </button>
        </>
      )}
    </header>
  );

  if (projects.length === 0 && !meta) {
    return (
      <div className="panel">
        {newProjectOpen && (
          <NewProjectDialog
            languages={languages}
            onClose={() => setNewProjectOpen(false)}
            onCreate={createProject}
          />
        )}
        {toolbar}
        <div className="empty">
          <span className="big">no projects yet</span>
          <span>Create your first snake to start editing.</span>
          <button className="btn primary" style={{ marginTop: 8 }} onClick={() => setNewProjectOpen(true)}>
            + New Project
          </button>
        </div>
      </div>
    );
  }

  const displayedFiles = viewMode === "submitted" ? submittedFiles : files;
  const displayedActivePath = viewMode === "submitted" ? submittedActivePath : activePath;
  const displayedActiveFile = displayedFiles.find((f) => f.path === displayedActivePath) ?? null;
  const isExternalImage = meta?.source === "external_image";

  const treePane = (
    <FileTree
      files={displayedFiles}
      activePath={displayedActivePath}
      dirtyPaths={viewMode === "submitted" ? new Set() : dirtyPaths}
      onOpen={(p) => {
        if (viewMode === "submitted") setSubmittedActivePath(p);
        else setActivePath(p);
        setMobileTab("editor");
      }}
      onAddFile={viewMode === "dev" ? addFile : undefined}
      onRenameFile={viewMode === "dev" ? renameFile : undefined}
      onDeleteFile={viewMode === "dev" ? deleteFile : undefined}
    />
  );

  const editorPane = (
    <div className="panel">
      <div className="panel-head">
        <span className="title">{displayedActivePath ?? "—"}</span>
        {loadingFiles && <span className="muted">loading…</span>}
        {viewMode === "submitted" && <span className="muted">submitted · read-only</span>}
      </div>
      <div className="panel-body" style={{ overflow: "hidden" }}>
        <CodeEditor
          path={displayedActivePath}
          value={displayedActiveFile?.content ?? ""}
          readOnly={viewMode === "submitted" || meta?.source !== "browser"}
          onChange={updateActive}
        />
      </div>
    </div>
  );

  const uploadPane = meta ? (
    <ImageUploadPanel meta={meta} languages={languages} onUploaded={setMeta} />
  ) : null;

  return (
    <div className="panel">
      {testDialogOpen && meta && (
        <TestDialog
          project={meta}
          initialSettings={loadTestSettings(meta.id)}
          languages={languages}
          quota={testQuota}
          onClose={() => setTestDialogOpen(false)}
          onRun={runTestMatch}
        />
      )}
      {newProjectOpen && (
        <NewProjectDialog
          languages={languages}
          onClose={() => setNewProjectOpen(false)}
          onCreate={createProject}
        />
      )}
      {toolbar}

      {/* Render only the layout for the current viewport — mounting both trees
          would mount two MatchViewers (two match WebSockets). */}
      {isMobile ? (
        <>
          <div className="mtabs panel-head">
            <div className="seg">
              {(isExternalImage
                ? (["editor", "viewer"] as MobileTab[])
                : (["files", "editor", "viewer"] as MobileTab[])
              ).map((t) => (
                <button key={t} className={mobileTab === t ? "on" : ""} onClick={() => setMobileTab(t)}>
                  {isExternalImage && t === "editor" ? "image" : t}
                </button>
              ))}
            </div>
          </div>
          <div className="panel-body" style={{ overflow: "hidden" }}>
            {!isExternalImage && mobileTab === "files" && treePane}
            {isExternalImage && mobileTab === "editor" ? uploadPane : mobileTab === "editor" && editorPane}
            {mobileTab === "viewer" && (
              <MatchViewer
                matchTabs={matchTabs}
                activeTabId={activeTabId}
                projectId={meta?.id ?? null}
                onTabSelect={onTabSelect}
                onTabClose={onTabClose}
                onOpenMatch={onOpenMatch}
                onMatchStatus={onMatchStatus}
                onBuildStatus={onBuildStatus}
                onJobPinChange={onJobPinChange}
                onJobsRefreshed={onJobsRefreshed}
              />
            )}
          </div>
        </>
      ) : (
        <div className="panel-body" style={{ overflow: "hidden" }}>
          <PanelGroup direction="horizontal">
            <Panel defaultSize={55} minSize={30}>
              {isExternalImage ? uploadPane : (
                <PanelGroup direction="horizontal">
                  <Panel defaultSize={28} minSize={14} maxSize={50}>
                    {treePane}
                  </Panel>
                  <PanelResizeHandle className="resize-handle" />
                  <Panel minSize={25}>{editorPane}</Panel>
                </PanelGroup>
              )}
            </Panel>
            <PanelResizeHandle className="resize-handle" />
            <Panel ref={viewerPanelRef} defaultSize={45} minSize={20}>
              <MatchViewer
                matchTabs={matchTabs}
                activeTabId={activeTabId}
                projectId={meta?.id ?? null}
                onTabSelect={onTabSelect}
                onTabClose={onTabClose}
                onOpenMatch={onOpenMatch}
                onMatchStatus={onMatchStatus}
                onBuildStatus={onBuildStatus}
                onJobPinChange={onJobPinChange}
                onJobsRefreshed={onJobsRefreshed}
              />
            </Panel>
          </PanelGroup>
        </div>
      )}
    </div>
  );
}
