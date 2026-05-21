import type { SimStartData, SimState } from "./types";

// One palette entry per snake. ID 0 (player) gets the accent green.
const PALETTES = [
  { head: "#b8ff3c", body: "#4d7a10" }, // player — accent
  { head: "#60a5fa", body: "#1d4ed8" }, // blue
  { head: "#f87171", body: "#b91c1c" }, // red
  { head: "#fb923c", body: "#c2410c" }, // orange
  { head: "#a78bfa", body: "#6d28d9" }, // purple
  { head: "#34d399", body: "#065f46" }, // teal
];

const DEAD_COLOR = "#5e646b";
const WALL_COLOR = "#2a2e35";
const FREE_COLOR = "#0c0e10";
const FOOD_COLOR = "#f5b740";
const GRID_LINE  = "rgba(255,255,255,0.03)";

export class SimRenderer {
  private ctx: CanvasRenderingContext2D;

  constructor(private canvas: HTMLCanvasElement) {
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Could not get 2D canvas context");
    this.ctx = ctx;
  }

  render(state: SimState, meta: SimStartData): void {
    const { width: gW, height: gH } = meta;
    const cW = this.canvas.width;
    const cH = this.canvas.height;
    const cellW = cW / gW;
    const cellH = cH / gH;

    const ctx = this.ctx;

    // Background
    ctx.fillStyle = FREE_COLOR;
    ctx.fillRect(0, 0, cW, cH);

    // Walls from base_map
    ctx.fillStyle = WALL_COLOR;
    for (let y = 0; y < gH; y++) {
      for (let x = 0; x < gW; x++) {
        if (meta.base_map[y]?.[x] === meta.blocked_value) {
          ctx.fillRect(x * cellW, y * cellH, cellW, cellH);
        }
      }
    }

    // Subtle grid lines
    ctx.strokeStyle = GRID_LINE;
    ctx.lineWidth = 0.5;
    for (let x = 0; x <= gW; x++) {
      ctx.beginPath();
      ctx.moveTo(x * cellW, 0);
      ctx.lineTo(x * cellW, cH);
      ctx.stroke();
    }
    for (let y = 0; y <= gH; y++) {
      ctx.beginPath();
      ctx.moveTo(0, y * cellH);
      ctx.lineTo(cW, y * cellH);
      ctx.stroke();
    }

    // Food
    const foodPad = Math.max(1, Math.floor(Math.min(cellW, cellH) * 0.2));
    ctx.fillStyle = FOOD_COLOR;
    for (const key of state.food) {
      const [fx, fy] = key.split(",").map(Number);
      ctx.beginPath();
      const cx = fx * cellW + cellW / 2;
      const cy = fy * cellH + cellH / 2;
      const r = Math.max(1, Math.min(cellW, cellH) / 2 - foodPad);
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.fill();
    }

    // Snakes — render bodies first, then heads on top
    const bodyPad = Math.max(1, Math.floor(Math.min(cellW, cellH) * 0.1));

    for (const [id, snake] of state.snakes) {
      const palette = PALETTES[id % PALETTES.length];

      // Body segments (skip head)
      ctx.fillStyle = snake.alive ? palette.body : DEAD_COLOR;
      for (let i = 1; i < snake.body.length; i++) {
        const [bx, by] = snake.body[i];
        ctx.fillRect(
          bx * cellW + bodyPad,
          by * cellH + bodyPad,
          cellW - bodyPad * 2,
          cellH - bodyPad * 2,
        );
      }

      // Head
      ctx.fillStyle = snake.alive ? palette.head : DEAD_COLOR;
      const [hx, hy] = snake.body[0];
      ctx.fillRect(
        hx * cellW + bodyPad,
        hy * cellH + bodyPad,
        cellW - bodyPad * 2,
        cellH - bodyPad * 2,
      );
    }
  }

  clear(): void {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
  }
}
