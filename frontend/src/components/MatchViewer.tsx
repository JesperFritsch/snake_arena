import { useState } from "react";
import type { BuildJob } from "../api/types";

type View = "runs" | "console";

interface Props {
  buildJob: BuildJob | null;
}

/**
 * Test-run viewer. Two views:
 *  - Runs: list of match/test runs (stub — needs a per-project runs endpoint).
 *  - Console: snake stdout / compilation output (stub — the backend does not
 *    yet surface build_logs or sim/agent logs through a read endpoint; only
 *    a build job's `error` string is available, shown here when present).
 */
export function MatchViewer({ buildJob }: Props) {
  const [view, setView] = useState<View>("runs");

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="title">Match Viewer</span>
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
        {view === "runs" ? (
          <div className="empty">
            <span className="big">no runs yet</span>
            <span style={{ maxWidth: 320 }}>
              Match playback lands here once the runs endpoint and replay viewer
              are wired up.
            </span>
          </div>
        ) : (
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
              <span className="muted">
                {`# console\n# build or run a snake to see output here.`}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
