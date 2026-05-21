// Types mirroring the JSON messages published by RedisStreamObserver.
// Keys are stringified integers (JSON only supports string keys in objects).

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
  final_step: number;
}

export interface SimLogsData {
  // seat index string → per-step stdout chunks (one entry per step)
  agent_logs: Record<string, string[]>;
}

export type SimMessage =
  | { type: "start"; data: SimStartData }
  | { type: "step";  data: SimStepData }
  | { type: "stop";  data: SimStopData }
  | { type: "logs";  data: SimLogsData }
  | { type: "error"; data: { message: string } };

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
