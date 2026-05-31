// Types mirroring the snake_sim model dumps published by RedisStreamObserver
// and stored in the replay. Keys are stringified integers (JSON only supports
// string keys in objects).

// Contents of LoopStartData.env_meta_data (EnvMetaData.model_dump).
export interface SimStartData {
  height: number;
  width: number;
  free_value: number;
  blocked_value: number;
  food_value: number;
  snake_tags: Record<string, string>;          // "0" → agent name
  snake_values: Record<string, { head_value: number; body_value: number }>;
  start_positions: Record<string, [number, number]>; // "0" → [x, y]
  base_map: number[][];                        // [row/y][col/x]
  base_map_dtype?: string;
}

export interface SimStepData {
  step: number;
  alive_states: Record<string, boolean>;
  decisions: Record<string, [number, number]>;
  tail_directions: Record<string, [number, number]>;
  snake_grew: Record<string, boolean>;
  lengths: Record<string, number>;
  new_food: [number, number][];
  removed_food: [number, number][];
}

export interface SimStopData {
  final_step: number | null;   // null when notify_stop never fired (sim aborted)
}

export type JobStatus = "queued" | "running" | "success" | "failure" | "cancelled";
// Mirrors dev_build_status: published live over the match channel.
export type BuildEvent = "building" | "built" | "ready" | "crashed" | "failed";

export type SimMessage =
  | { type: "snapshot";  data: { job_status: JobStatus | "unknown"; build_status: string | null; error: string | null } }
  | { type: "build";     data: { status: BuildEvent; error?: string } }
  | { type: "status";    data: { status: JobStatus } }
  // seat_by_snake_id: sim snake_id (string) -> runner seat. Backend may omit
  // it for very old replays / when target_by_seat was never wired; consumer
  // falls back to identity (snake_id == seat), which is wrong if the sim
  // assigns non-sequential ids but matches pre-change behavior.
  | { type: "start";     data: { env_meta_data: SimStartData; seat_by_snake_id?: Record<string, number> } }
  | { type: "step";      data: SimStepData }
  | { type: "stop";      data: SimStopData }
  | { type: "step_log";  data: { step: number; log: string } }
  // times: seat (as a stringified int) -> CPU ms this step.
  | { type: "exec_time"; data: { step: number; times: Record<string, number> } }
  | { type: "error";     data: { message: string } };

// Reconstructed per-step state used by the renderer.
export interface SnakeState {
  body: [number, number][]; // head at index 0
  alive: boolean;
}

export interface SimState {
  step: number;
  snakes: Map<number, SnakeState>;
  food: Set<string>; // "${x},${y}"
}
