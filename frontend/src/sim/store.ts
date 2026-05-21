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

  get stepCount(): number {
    return this.steps.length;
  }

  get isComplete(): boolean {
    return this.finalStep !== null;
  }

  addMessage(msg: SimMessage): void {
    switch (msg.type) {
      case "start":
        this.startData = msg.data;
        this.steps = [];
        this.finalStep = null;
        break;
      case "step":
        this.steps.push(msg.data);
        break;
      case "stop":
        this.finalStep = msg.data.final_step;
        break;
    }
  }

  /** Reconstruct game state at a given step index (0-based). */
  getStateAtStep(stepIndex: number): SimState | null {
    if (!this.startData) return null;
    const clampedIndex = Math.max(0, Math.min(stepIndex, this.steps.length - 1));

    // Build initial state from start positions
    const snakes = new Map<number, SnakeState>();
    for (const [idStr, pos] of Object.entries(this.startData.start_positions)) {
      snakes.set(Number(idStr), { body: [[pos[0], pos[1]]], alive: true });
    }
    const food = new Set<string>();

    // Apply steps 0 .. clampedIndex
    for (let i = 0; i <= clampedIndex; i++) {
      _applyStep(snakes, food, this.steps[i]);
    }

    return { step: clampedIndex, snakes, food };
  }

  reset(): void {
    this.startData = null;
    this.steps = [];
    this.finalStep = null;
    this.annotations = [];
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
