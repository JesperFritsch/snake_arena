import { useEffect, useRef, useState } from "react";
import { SimStore } from "./store";
import {
  fetchBundleFiles,
  populateStoreFromBundle,
  extractHighlightsFromBundle,
} from "./bundleLoader";
import type { Highlight } from "./highlights";
import type { SimSource, SimSourceStatus } from "./source";

interface Options {
  /** Returns a signed URL for the bundle zip. */
  getBundleUrl: () => Promise<{ url: string }>;
  /** Key used to re-run the load when the source changes (e.g. matchId). */
  resetKey?: string | number;
}

export function useBundleSource({ getBundleUrl, resetKey }: Options): SimSource {
  const storeRef = useRef(new SimStore());

  const [status, setStatus] = useState<SimSourceStatus>("loading");
  const [errorMsg, setErrorMsg] = useState("");
  const [totalSteps, setTotalSteps] = useState(0);
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  const [gridSize, setGridSize] = useState<{ width: number; height: number } | null>(null);

  // getBundleUrl is generally a closure that changes identity every render —
  // pin it via a ref so the load effect's only trigger is resetKey.
  const getBundleUrlRef = useRef(getBundleUrl);
  getBundleUrlRef.current = getBundleUrl;

  useEffect(() => {
    let cancelled = false;
    storeRef.current.reset();
    setStatus("loading");
    setErrorMsg("");
    setTotalSteps(0);
    setHighlights([]);
    setGridSize(null);

    (async () => {
      try {
        const files = await fetchBundleFiles(getBundleUrlRef.current);
        if (cancelled) return;
        const { gridSize: gs } = populateStoreFromBundle(storeRef.current, files);
        const hl = extractHighlightsFromBundle(files);
        if (cancelled) return;
        setGridSize(gs);
        setHighlights(hl);
        setTotalSteps(storeRef.current.frameCount);
        setStatus("ended");
      } catch (e) {
        if (cancelled) return;
        setErrorMsg(e instanceof Error ? e.message : "failed to load replay");
        setStatus("error");
      }
    })();

    return () => { cancelled = true; };
  }, [resetKey]);

  return {
    store: storeRef.current,
    status,
    errorMsg,
    totalSteps,
    highlights,
    gridSize,
    liveTick: 0, // bundle data is fully present after load; no live ticks
  };
}
