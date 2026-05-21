import { useCallback, useEffect, useRef, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle, type ImperativePanelHandle } from "react-resizable-panels";
import { useApi, ApiError } from "../api/client";
import type { BuildJob, ProjectFile, ProjectMeta, TestMatchJob } from "../api/types";
import { FileTree } from "../components/FileTree";
import { CodeEditor } from "../components/CodeEditor";
import { MatchViewer } from "../components/MatchViewer";
import { TestDialog } from "../components/TestDialog";
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

  const [languages, setLanguages] = useState<string[]>([]);
  const [projects, setProjects] = useState<ProjectMeta[]>([]);
  const [meta, setMeta] = useState<ProjectMeta | null>(null);
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [originalSig, setOriginalSig] = useState<string>("[]");
  const [activePath, setActivePath] = useState<string | null>(null);

  const [viewMode, setViewMode] = useState<"dev" | "submitted">("dev");
  const [submittedFiles, setSubmittedFiles] = useState<ProjectFile[]>([]);
  const [submittedActivePath, setSubmittedActivePath] = useState<string | null>(null);

  const [buildJob, setBuildJob] = useState<BuildJob | null>(null);
  const [testMatchJob, setTestMatchJob] = useState<TestMatchJob | null>(null);
  const [testDialogOpen, setTestDialogOpen] = useState(false);
  const [busy, setBusy] = useState<"" | "save" | "build" | "submit" | "delete" | "restore">("");
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [mobileTab, setMobileTab] = useState<MobileTab>("editor");

  const pollRef       = useRef<number | null>(null);
  const testPollRef   = useRef<number | null>(null);
  const viewerPanelRef = useRef<ImperativePanelHandle>(null);

  const dirty = serialize(files) !== originalSig;
  const dirtyPaths = new Set(
    files
      .filter((f) => {
        const orig = JSON.parse(originalSig) as [string, string][];
        const match = orig.find(([p]) => p === f.path);
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
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      if (testPollRef.current) window.clearInterval(testPollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectProject = useCallback(
    async (id: number, knownList?: ProjectMeta[]) => {
      const list = knownList ?? projects;
      const m = list.find((p) => p.id === id) ?? (await api.getProject(id));
      setMeta(m);
      setBuildJob(null);
      setTestMatchJob(null);
      if (testPollRef.current) { window.clearInterval(testPollRef.current); testPollRef.current = null; }
      setViewMode("dev");
      setSubmittedFiles([]);
      setSubmittedActivePath(null);
      setLoadingFiles(true);
      try {
        const { files: fetched } = await api.getFiles(id);
        setFiles(fetched);
        setOriginalSig(serialize(fetched));
        setActivePath(fetched[0]?.path ?? null);
      } catch (e) {
        // external_image projects (and brand-new ones) may have no editable code
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

  const addFile = () => {
    const path = window.prompt("New file path (e.g. src/util.py)")?.trim();
    if (!path) return;
    if (files.some((f) => f.path === path)) {
      push("A file with that path already exists.", "error");
      return;
    }
    setFiles((fs) => [...fs, { path, content: "", encoding: "utf-8" }]);
    setActivePath(path);
    setMobileTab("editor");
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

  const pollBuild = useCallback(
    (jobId: number) => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = window.setInterval(async () => {
        try {
          const job = await api.getBuildJob(jobId);
          setBuildJob(job);
          if (TERMINAL.has(job.status)) {
            if (pollRef.current) window.clearInterval(pollRef.current);
            pollRef.current = null;
            if (meta) api.getProject(meta.id).then(setMeta).catch(() => {});
            push(
              job.status === "success" ? "Build succeeded." : `Build ${job.status}.`,
              job.status === "success" ? "info" : "error",
            );
          }
        } catch (e) {
          if (pollRef.current) window.clearInterval(pollRef.current);
          pollRef.current = null;
          push(`Build polling failed: ${e instanceof ApiError ? e.detail : e}`, "error");
        }
      }, 1200);
    },
    [api, meta, push],
  );

  const pollTestMatch = useCallback(
    (jobId: number) => {
      if (testPollRef.current) window.clearInterval(testPollRef.current);
      testPollRef.current = window.setInterval(async () => {
        try {
          const job = await api.getTestMatchJob(jobId);
          setTestMatchJob(job);
          if (TERMINAL.has(job.status)) {
            if (testPollRef.current) window.clearInterval(testPollRef.current);
            testPollRef.current = null;
            push(
              job.status === "success" ? "Test match finished." : `Test match ${job.status}.`,
              job.status === "success" ? "info" : "error",
            );
          }
        } catch (e) {
          if (testPollRef.current) window.clearInterval(testPollRef.current);
          testPollRef.current = null;
          push(`Test match polling failed: ${e instanceof ApiError ? e.detail : e}`, "error");
        }
      }, 1500);
    },
    [api, push],
  );

  const onTestMatchEnqueued = (job: TestMatchJob) => {
    setTestMatchJob(job);
    setMobileTab("viewer");
    pollTestMatch(job.id);
    push(`Test match #${job.id} queued.`);
    // Expand the viewer panel past the WIDE_THRESHOLD so the layout snaps to
    // side-by-side (player left, console right) automatically.
    viewerPanelRef.current?.resize(52);
  };

  const build = async () => {
    if (!meta) return;
    if (dirty && !(await save())) return; // build uses server-stored code
    setBusy("build");
    try {
      const res = await api.build(meta.id);
      setBuildJob(res.job ?? null);
      setMobileTab("viewer");
      pollBuild(res.build_job_id);
      push(`Build #${res.build_job_id} queued.`);
    } catch (e) {
      push(`Build failed to enqueue: ${e instanceof ApiError ? e.detail : e}`, "error");
    } finally {
      setBusy("");
    }
  };

  const submit = async () => {
    if (!meta) return;
    setBusy("submit");
    try {
      const res = await api.submit(meta.id);
      push(`Submitted as version ${res.submitted_version}.`);
      api.getProject(meta.id).then(setMeta).catch(() => {});
    } catch (e) {
      // 409 is the expected "test your latest changes first" outcome
      push(e instanceof ApiError ? e.detail : String(e), "error");
    } finally {
      setBusy("");
    }
  };

  const createProject = async () => {
    const name = window.prompt("Project name")?.trim();
    if (!name) return;
    const langHint = languages.length ? languages.join(" / ") : "e.g. python";
    const language = (window.prompt(`Language (${langHint})`, languages[0] ?? "") ?? "")
      .trim()
      .toLowerCase();
    if (!language) return;
    try {
      const created = await api.createProject({
        name,
        language,
        source: "browser",
      });
      const next = [created, ...projects];
      setProjects(next);
      await selectProject(created.id, next);
      push(`Created “${name}”.`);
    } catch (e) {
      push(`Could not create project: ${e instanceof ApiError ? e.detail : e}`, "error");
    }
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
        setBuildJob(null);
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
    <header className="panel-head" style={{ height: 46 }}>
      <select
        className="select"
        value={meta?.id ?? ""}
        onChange={(e) => selectProject(Number(e.target.value))}
      >
        {projects.length === 0 && <option value="">No projects</option>}
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name} · {p.language}
          </option>
        ))}
      </select>
      <button className="btn ghost" onClick={createProject}>
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
      {meta && meta.dev_build_status === "ready" && viewMode === "dev" && (
        <button className="btn ghost" onClick={() => setTestDialogOpen(true)}>
          Test
        </button>
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
          <button className="btn" disabled={!meta || !dirty || busy === "save"} onClick={save}>
            {busy === "save" ? "Saving…" : dirty ? "Save" : "Saved"}
          </button>
          <button
            className="btn"
            disabled={!meta || busy === "build" || buildJob?.status === "running"}
            onClick={build}
          >
            {busy === "build" || buildJob?.status === "running" ? "Building…" : "Build"}
          </button>
          <button className="btn primary" disabled={!meta || busy === "submit"} onClick={submit}>
            {busy === "submit" ? "Submitting…" : "Submit"}
          </button>
        </>
      )}
    </header>
  );

  if (projects.length === 0 && !meta) {
    return (
      <div className="panel">
        {toolbar}
        <div className="empty">
          <span className="big">no projects yet</span>
          <span>Create your first snake to start editing.</span>
          <button className="btn primary" style={{ marginTop: 8 }} onClick={createProject}>
            + New Project
          </button>
        </div>
      </div>
    );
  }

  const displayedFiles = viewMode === "submitted" ? submittedFiles : files;
  const displayedActivePath = viewMode === "submitted" ? submittedActivePath : activePath;
  const displayedActiveFile = displayedFiles.find((f) => f.path === displayedActivePath) ?? null;

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

  return (
    <div className="panel">
      {testDialogOpen && meta && (
        <TestDialog
          project={meta}
          onClose={() => setTestDialogOpen(false)}
          onEnqueued={onTestMatchEnqueued}
        />
      )}
      {toolbar}

      {/* mobile tab switcher */}
      <div className="mtabs panel-head">
        <div className="seg">
          {(["files", "editor", "viewer"] as MobileTab[]).map((t) => (
            <button key={t} className={mobileTab === t ? "on" : ""} onClick={() => setMobileTab(t)}>
              {t}
            </button>
          ))}
        </div>
      </div>

      {/* desktop: tree | editor || match viewer, all resizable */}
      <div className="panel-body desktop-only" style={{ overflow: "hidden" }}>
        <PanelGroup direction="horizontal">
          <Panel defaultSize={55} minSize={30}>
            <PanelGroup direction="horizontal">
              <Panel defaultSize={28} minSize={14} maxSize={50}>
                {treePane}
              </Panel>
              <PanelResizeHandle className="resize-handle" />
              <Panel minSize={25}>{editorPane}</Panel>
            </PanelGroup>
          </Panel>
          <PanelResizeHandle className="resize-handle" />
          <Panel ref={viewerPanelRef} defaultSize={45} minSize={20}>
            <MatchViewer buildJob={buildJob} testMatchJob={testMatchJob} />
          </Panel>
        </PanelGroup>
      </div>

      {/* mobile: one pane at a time */}
      <div className="panel-body mtabs-body" style={{ overflow: "hidden" }}>
        {mobileTab === "files" && treePane}
        {mobileTab === "editor" && editorPane}
        {mobileTab === "viewer" && <MatchViewer buildJob={buildJob} testMatchJob={testMatchJob} />}
      </div>
    </div>
  );
}
