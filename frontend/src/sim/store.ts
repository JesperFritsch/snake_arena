import type { SimMessage, SimStartData, SimStepData, SimState, SnakeState } from "./types";

/**
 * Accumulates sim messages and reconstructs game state at any step.
 *
 * State reconstruction is O(n) where n = target step number.
 * For typical game lengths (≤ 2000 steps) this is imperceptibly fast.
 *
 * The `annotations` array is intentionally kept empty for now — it is the
 * future slot for run_analyzer highlights rendered on the playback timeline.
 */
export interface Annotation {
  step: number;
  type: string;
  data: unknown;
}

export class SimStore {
  startData: SimStartData | null = null;
  private steps: SimStepData[] = [];
  private finalStep: number | null = null;
  annotations: Annotation[] = [];
  private agentLogs: Record<string, string[]> | null = null;
  private execTimesPerStep: Record<string, number>[] = [];

  /** Renderable frames: frame 0 is the start state, then one per step. */
  get frameCount(): number {
    return this.startData ? this.steps.length + 1 : 0;
  }

  get isComplete(): boolean {
    return this.finalStep !== null;
  }

  addMessage(msg: SimMessage): void {
    switch (msg.type) {
      case "start":
        this.startData = msg.data.env_meta_data;
        this.steps = [];
        this.finalStep = null;
        break;
      case "step":
        this.steps.push(msg.data);
        break;
      case "stop":
        this.finalStep = msg.data.final_step;
        break;
      case "step_log": {
        if (!this.agentLogs) this.agentLogs = { "0": [] };
        const chunks = this.agentLogs["0"] ?? (this.agentLogs["0"] = []);
        chunks[msg.data.step] = msg.data.log;
        break;
      }
      case "exec_time":
        this.execTimesPerStep[msg.data.step] = msg.data.times;
        break;
    }
  }

  /** Returns seat 0's stdout chunk for the given step, or null if unavailable. */
  getDevLogs(stepIndex: number): string | null {
    const chunks = this.agentLogs?.["0"];
    if (!chunks) return null;
    return chunks[stepIndex] ?? null;
  }

  /** Returns per-snake CPU times (ms) for the given step, or null if unavailable. */
  getExecTimes(stepIndex: number): Record<string, number> | null {
    return this.execTimesPerStep[stepIndex] ?? null;
  }

  /** Reconstruct the rendered state at a frame. Frame 0 is the start state
   *  (heads at start positions, all alive); frame S is the state after the
   *  first S steps have been applied. */
  getStateAtStep(frameIndex: number): SimState | null {
    if (!this.startData) return null;
    const frame = Math.max(0, Math.min(frameIndex, this.steps.length));

    // Frame 0: snakes at their start positions, no steps applied.
    const snakes = new Map<number, SnakeState>();
    for (const [idStr, pos] of Object.entries(this.startData.start_positions)) {
      snakes.set(Number(idStr), { body: [[pos[0], pos[1]]], alive: true });
    }
    const food = new Set<string>();

    // Apply the first `frame` steps (frame 0 applies none).
    for (let i = 0; i < frame; i++) {
      _applyStep(snakes, food, this.steps[i]);
    }

    return { step: frame, snakes, food };
  }

  reset(): void {
    this.startData = null;
    this.steps = [];
    this.finalStep = null;
    this.annotations = [];
    this.agentLogs = null;
    this.execTimesPerStep = [];
  }
}

function _applyStep(
  snakes: Map<number, SnakeState>,
  food: Set<string>,
  step: SimStepData,
): void {
  // Food updates
  for (const [fx, fy] of step.new_food) food.add(`${fx},${fy}`);
  for (const [fx, fy] of step.removed_food) food.delete(`${fx},${fy}`);

  // Snake updates — only alive snakes have decisions/tail_directions
  for (const [idStr, alive] of Object.entries(step.alive_states)) {
    const id = Number(idStr);
    const snake = snakes.get(id);
    if (!snake) continue;

    snake.alive = alive;
    if (!alive) continue;

    const decision = step.decisions[idStr];
    if (!decision) continue;

    const [hx, hy] = snake.body[0];
    snake.body.unshift([hx + decision[0], hy + decision[1]]);

    // Pop tail unless it stayed in place (snake grew, tail_direction = [0,0])
    const td = step.tail_directions[idStr];
    if (td && (td[0] !== 0 || td[1] !== 0)) {
      snake.body.pop();
    }
  }
}
