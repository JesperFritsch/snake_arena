import { useCallback, useRef } from "react";
import { useAuth } from "@clerk/clerk-react";
import { useApi } from "../api/client";
import { useLiveSource } from "../sim/useLiveSource";
import type { SimStore } from "../sim/store";
import type { TestMatchJob } from "../api/types";
import { SimPlayer } from "./SimPlayer";

interface Props {
  job: TestMatchJob;
  /** Dev-agent stdout for the currently rendered step (or null). */
  onConsoleLog?: (log: string | null) => void;
  /** Per-snake CPU times for the currently rendered step (or null). */
  onExecTimes?: (times: Record<string, number> | null) => void;
  /** Live job status arrivals (running / success / failure / cancelled). */
  onJobStatus?: (status: string) => void;
  /** Live build status arrivals (building / built / ready / crashed / failed). */
  onBuildStatus?: (status: string) => void;
}

/** Plays a (potentially) in-progress test match: streams over WS while the
 *  job is queued/running, falls back to the bundle if attached after the
 *  match already finished. */
export function LiveSimPlayer({
  job,
  onConsoleLog,
  onExecTimes,
  onJobStatus,
  onBuildStatus,
}: Props) {
  const api = useApi();
  const { getToken, isSignedIn } = useAuth();

  // Pin the API getter so the live source's effect deps stay minimal.
  const apiRef = useRef(api);
  apiRef.current = api;
  const getBundleUrl = useCallback(
    () => apiRef.current.getTestMatchBundleUrl(job.id),
    [job.id],
  );

  const source = useLiveSource({
    jobId: job.id,
    jobStatus: job.status,
    jobError: job.error,
    isSignedIn: isSignedIn === true,
    getToken,
    getBundleUrl,
    onJobStatus,
    onBuildStatus,
  });

  // Translate step changes / liveTick into the parent's step-keyed callbacks.
  const onConsoleLogRef = useRef(onConsoleLog);
  onConsoleLogRef.current = onConsoleLog;
  const onExecTimesRef = useRef(onExecTimes);
  onExecTimesRef.current = onExecTimes;

  const handleStepChange = useCallback((step: number, store: SimStore) => {
    onConsoleLogRef.current?.(store.getDevLogs(step));
    onExecTimesRef.current?.(store.getExecTimes(step));
  }, []);

  return (
    <SimPlayer
      source={source}
      participantNames={job.participant_names}
      // Seat 0 is the user's own agent — flag it in the legend and labels.
      labelForSeat={(seat, name) => (seat === 0 ? `${name} (dev)` : name)}
      showLiveBadge
      onStepChange={handleStepChange}
    />
  );
}
