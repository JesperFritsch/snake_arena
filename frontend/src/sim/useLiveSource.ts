import { useEffect, useRef, useState } from "react";
import { SimStore } from "./store";
import {
  fetchBundleFiles,
  populateStoreFromBundle,
  extractHighlightsFromBundle,
} from "./bundleLoader";
import type { Highlight } from "./highlights";
import type { SimMessage } from "./types";
import type { SimSource, SimSourceStatus } from "./source";

const BASE_WS_URL = (import.meta.env.VITE_API_BASE_URL ?? "")
  .replace(/\/$/, "")
  .replace(/^http/, "ws");

interface Options {
  jobId: number;
  /** Current status when the hook mounts. Drives the initial transport choice
   *  (queued/running → WS; success → bundle; failure/cancelled → failed). */
  jobStatus: string;
  jobError: string | null;
  /** Clerk-style getter for the WS auth token. */
  getToken: () => Promise<string | null>;
  getBundleUrl: () => Promise<{ url: string }>;
  /** Forwards JobStatus arrivals over the live channel. */
  onJobStatus?: (status: string) => void;
  /** Forwards dev_build_status arrivals (from snapshot + build messages). */
  onBuildStatus?: (status: string) => void;
}

export function useLiveSource(opts: Options): SimSource {
  const { jobId, jobStatus, jobError, getToken, getBundleUrl, onJobStatus, onBuildStatus } = opts;

  const storeRef = useRef(new SimStore());

  const [status, setStatus] = useState<SimSourceStatus>("connecting");
  const [errorMsg, setErrorMsg] = useState("");
  const [totalSteps, setTotalSteps] = useState(0);
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  const [gridSize, setGridSize] = useState<{ width: number; height: number } | null>(null);
  const [liveTick, setLiveTick] = useState(0);

  // Pin closures so the connect effect doesn't restart when callers re-render.
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const getBundleUrlRef = useRef(getBundleUrl);
  getBundleUrlRef.current = getBundleUrl;
  const onJobStatusRef = useRef(onJobStatus);
  onJobStatusRef.current = onJobStatus;
  const onBuildStatusRef = useRef(onBuildStatus);
  onBuildStatusRef.current = onBuildStatus;

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;

    storeRef.current.reset();
    setStatus("connecting");
    setErrorMsg("");
    setTotalSteps(0);
    setHighlights([]);
    setGridSize(null);
    setLiveTick(0);

    // ── Terminal pre-conditions: surface the failure and skip the WS dance.
    if (jobStatus === "failure") {
      setErrorMsg(jobError ?? "match failed");
      setStatus("failed");
      return;
    }
    if (jobStatus === "cancelled") {
      setErrorMsg("test match was cancelled");
      setStatus("failed");
      return;
    }

    // ── Already-finished job → no live stream to attach to; load the bundle.
    const loadBundle = async () => {
      setStatus("loading");
      try {
        const files = await fetchBundleFiles(getBundleUrlRef.current);
        if (cancelled) return;
        storeRef.current.reset();
        const { gridSize: gs } = populateStoreFromBundle(storeRef.current, files);
        const hl = extractHighlightsFromBundle(files);
        if (cancelled) return;
        setGridSize(gs);
        setHighlights(hl);
        setTotalSteps(storeRef.current.frameCount);
        setStatus("ended");
        setLiveTick((t) => t + 1);
      } catch (e) {
        if (cancelled) return;
        setErrorMsg(e instanceof Error ? e.message : "failed to load bundle");
        setStatus("error");
      }
    };

    if (jobStatus !== "queued" && jobStatus !== "running") {
      void loadBundle();
      return () => { cancelled = true; };
    }

    // ── Live path: connect WS and stream messages into the store.
    getTokenRef.current().then((token) => {
      if (cancelled || !token) return;
      ws = new WebSocket(
        `${BASE_WS_URL}/test-matches/${jobId}/ws?token=${encodeURIComponent(token)}`,
      );

      ws.onmessage = (evt) => {
        const msg = JSON.parse(evt.data as string) as SimMessage;
        storeRef.current.addMessage(msg);

        if (msg.type === "snapshot") {
          if (msg.data.build_status) onBuildStatusRef.current?.(msg.data.build_status);
          if (["success", "failure", "cancelled"].includes(msg.data.job_status)) {
            // We attached after the match already finished — no live stream
            // to consume; fall back to bundle. (A live stream that *ends*
            // mid-session does NOT come here; see msg.type === "stop".)
            ws?.close();
            if (msg.data.job_status === "failure") {
              setErrorMsg(msg.data.error ?? "match failed");
              setStatus("failed");
            } else {
              void loadBundle();
            }
          }
        } else if (msg.type === "build") {
          onBuildStatusRef.current?.(msg.data.status);
          if (msg.data.status === "failed") {
            setErrorMsg(msg.data.error ?? "build failed");
            setStatus("failed");
          }
        } else if (msg.type === "status") {
          onJobStatusRef.current?.(msg.data.status);
          if (msg.data.status === "success") {
            // The live stream is finishing — pick up analysis.json (death/trap
            // markers) without resetting the in-memory replay the user just
            // watched. Highlights are non-critical so failures are swallowed.
            void (async () => {
              try {
                const files = await fetchBundleFiles(getBundleUrlRef.current);
                if (cancelled) return;
                setHighlights(extractHighlightsFromBundle(files));
              } catch { /* ignore */ }
            })();
          }
        } else if (msg.type === "start") {
          const d = msg.data.env_meta_data;
          setGridSize({ width: d.width, height: d.height });
          setStatus("live");
          setTotalSteps(storeRef.current.frameCount); // frame 0 = start state
        } else if (msg.type === "step") {
          setTotalSteps(storeRef.current.frameCount);
        } else if (msg.type === "step_log" || msg.type === "exec_time") {
          setLiveTick((t) => t + 1);
        } else if (msg.type === "stop") {
          setStatus("ended");
        } else if (msg.type === "error") {
          setErrorMsg(msg.data.message);
          setStatus("failed");
        }
      };

      ws.onerror = () => { if (!cancelled) setStatus("error"); };
      ws.onclose = () => {
        if (!cancelled) setStatus((s) => (s === "live" ? "ended" : s));
      };
    });

    return () => {
      cancelled = true;
      ws?.close();
    };
  // jobError/jobStatus snapshot is read once per mount — re-running on every
  // parent status update would tear down the WS that's about to send those
  // very updates. Only jobId changes warrant a reset.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  return {
    store: storeRef.current,
    status,
    errorMsg,
    totalSteps,
    highlights,
    gridSize,
    liveTick,
  };
}
