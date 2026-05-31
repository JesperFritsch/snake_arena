import { unzipSync } from "fflate";
import type { SimMessage } from "./types";
import { SimStore } from "./store";
import { type Highlight, parseHighlights } from "./highlights";

export type BundleFiles = Record<string, Uint8Array>;

/** Resolves a bundle URL (with retries — covers the brief window between a
 *  match finishing and the bundle being uploaded), fetches the zip, and
 *  returns its unpacked files. */
export async function fetchBundleFiles(
  getBundleUrl: () => Promise<{ url: string }>,
  { retries = 8, retryDelayMs = 750 }: { retries?: number; retryDelayMs?: number } = {},
): Promise<BundleFiles> {
  let urlResult: { url: string } | undefined;
  for (let i = 0; ; i++) {
    try {
      urlResult = await getBundleUrl();
      break;
    } catch (e) {
      if (i >= retries) throw e;
      await new Promise((r) => setTimeout(r, retryDelayMs));
    }
  }
  const resp = await fetch(urlResult!.url);
  if (!resp.ok) {
    throw new Error(
      resp.status === 404
        ? "replay recording not found — it may have been deleted from storage"
        : `failed to fetch replay (${resp.status})`,
    );
  }
  return unzipSync(new Uint8Array(await resp.arrayBuffer()));
}

export interface PopulateResult {
  gridSize: { width: number; height: number } | null;
}

/** Replays bundle contents into a SimStore: replay.json (start/step/stop),
 *  seat_by_snake_id.json sidecar, agent_logs.json (seat 0 dev console),
 *  exec_times.json. Returns the grid size extracted from the start frame. */
export function populateStoreFromBundle(store: SimStore, files: BundleFiles): PopulateResult {
  const replayText = new TextDecoder().decode(files["replay.json"]);
  const messages: SimMessage[] = replayText
    .split("\n")
    .filter((l) => l.trim() !== "")
    .map((l) => JSON.parse(l) as SimMessage);

  let gridSize: PopulateResult["gridSize"] = null;
  for (const msg of messages) {
    store.addMessage(msg);
    if (msg.type === "start") {
      const d = msg.data.env_meta_data;
      gridSize = { width: d.width, height: d.height };
    }
  }

  // replay.json doesn't carry the runner-side seat mapping; overlay the
  // sidecar (when present) over the identity fallback the store wrote.
  const seatFile = files["seat_by_snake_id.json"];
  if (seatFile) {
    const raw = JSON.parse(new TextDecoder().decode(seatFile)) as Record<string, number>;
    const m = new Map<number, number>();
    for (const [sid, seat] of Object.entries(raw)) m.set(Number(sid), seat);
    store.seatBySnakeId = m;
  }

  // Dev agent logs are a separate file (seat 0 only) to keep replay.json lean.
  const agentLogsFile = files["agent_logs.json"];
  if (agentLogsFile) {
    const agentLogs = JSON.parse(new TextDecoder().decode(agentLogsFile)) as Record<string, string[]>;
    (agentLogs["0"] ?? []).forEach((log, step) => {
      store.addMessage({ type: "step_log", data: { step, log } });
    });
  }

  const execTimesFile = files["exec_times.json"];
  if (execTimesFile) {
    const execTimesData = JSON.parse(new TextDecoder().decode(execTimesFile)) as Record<string, number[]>;
    const stepCount = Math.max(0, ...Object.values(execTimesData).map((a) => a.length));
    for (let step = 0; step < stepCount; step++) {
      const times: Record<string, number> = {};
      for (const [snakeId, arr] of Object.entries(execTimesData)) {
        if (arr[step] !== undefined) times[snakeId] = arr[step];
      }
      store.addMessage({ type: "exec_time", data: { step, times } });
    }
  }

  return { gridSize };
}

/** Parses analysis.json if present. Highlights are non-critical — returns []
 *  on missing file or parse failure rather than throwing. */
export function extractHighlightsFromBundle(files: BundleFiles): Highlight[] {
  const analysisFile = files["analysis.json"];
  if (!analysisFile) return [];
  try {
    return parseHighlights(analysisFile);
  } catch {
    return [];
  }
}
