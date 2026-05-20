import { useState } from "react";
import type { BuildJob, TestMatchJob } from "../api/types";

type View = "runs" | "console";

interface Props {
  buildJob: BuildJob | null;
  testMatchJob: TestMatchJob | null;
}

const STATUS_CLASS: Record<string, string> = {
  success: "ready",
  failure: "failed",
  running: "building",
  queued: "queued",
};

export function MatchViewer({ buildJob, testMatchJob }: Props) {
  const [view, setView] = useState<View>("runs");

  const runContent = testMatchJob ? (
    <div className="console">
      <span className="muted">{`# test match #${testMatchJob.id}\n`}</span>
      <span className={STATUS_CLASS[testMatchJob.status] === "failed" ? "err" : "muted"}>
        {`status: ${testMatchJob.status}\n`}
      </span>
      {testMatchJob.opponent_project_ids.length === 0 ? (
        <span className="muted">solo run (no opponents)\n</span>
      ) : (
        <span className="muted">{`opponents: project ids [${testMatchJob.opponent_project_ids.join(", ")}]\n`}</span>
      )}
      {testMatchJob.match_id && (
        <span className="muted">{`match id: ${testMatchJob.match_id}\n`}</span>
      )}
      {testMatchJob.error && (
        <span className="err">{testMatchJob.error}</span>
      )}
      {!TERMINAL_STATUSES.has(testMatchJob.status) && (
        <span className="muted">waiting for result…</span>
      )}
    </div>
  ) : (
    <div className="empty">
      <span className="big">no runs yet</span>
      <span style={{ maxWidth: 300 }}>
        Build your agent, then click Test to run a match.
      </span>
    </div>
  );

  const consoleContent = (
    <div className="console">
      {buildJob?.status === "failure" && buildJob.error ? (
        <>
          <span className="muted">{`# build #${buildJob.id} failed\n`}</span>
          <span className="err">{buildJob.error}</span>
        </>
      ) : buildJob ? (
        <span className="muted">
          {`# build #${buildJob.id} — ${buildJob.status}\n`}
          {`# stdout/compilation logs will stream here once the API exposes them.`}
        </span>
      ) : (
        <span className="muted">{`# console\n# build or run a snake to see output here.`}</span>
      )}
    </div>
  );

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="title">Match Viewer</span>
        {testMatchJob && (
          <span className={`pill ${STATUS_CLASS[testMatchJob.status] ?? ""}`}>
            <span className="dot" />
            {testMatchJob.status}
          </span>
        )}
        <span className="spacer" />
        <div className="seg">
          <button className={view === "runs" ? "on" : ""} onClick={() => setView("runs")}>
            Runs
          </button>
          <button className={view === "console" ? "on" : ""} onClick={() => setView("console")}>
            Console
          </button>
        </div>
      </div>

      <div className="panel-body">
        {view === "runs" ? runContent : consoleContent}
      </div>
    </div>
  );
}

const TERMINAL_STATUSES = new Set(["success", "failure", "cancelled"]);
