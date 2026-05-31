import type { SimStore } from "./store";
import type { Highlight } from "./highlights";

export type SimSourceStatus =
  | "connecting"  // WS handshake (live only)
  | "loading"     // fetching/unpacking bundle
  | "live"        // streaming steps from WS
  | "ended"       // no more data; in-memory store is final
  | "failed"      // semantic failure (build crashed, match errored)
  | "error";      // transport failure (fetch / WS / unzip)

export interface SimSource {
  store: SimStore;
  status: SimSourceStatus;
  errorMsg: string;
  /** SimStore.frameCount mirrored as React state so consumers re-render
   *  when new frames arrive on a live stream. */
  totalSteps: number;
  highlights: Highlight[];
  gridSize: { width: number; height: number } | null;
  /** Bumps whenever step_log / exec_time data arrives. Lets a consumer
   *  (e.g. the dev console wrapper) re-read store data for the current step
   *  without having to subscribe to raw messages. */
  liveTick: number;
}
