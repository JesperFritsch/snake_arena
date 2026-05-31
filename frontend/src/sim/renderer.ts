import type { SimStartData, SimState } from "./types";
import { colorForSeat } from "./colors";

const DEAD_COLOR = "#5e646b";
const WALL_COLOR = "#2a2e35";
const FREE_COLOR = "#0c0e10";
const FOOD_COLOR = "#f5b740";
const GRID_LINE  = "rgba(255,255,255,0.03)";
const TUBE_RATIO = 0.6; // tube width as a fraction of the smaller cell dimension

export class SimRenderer {
  private ctx: CanvasRenderingContext2D;

  constructor(private canvas: HTMLCanvasElement) {
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Could not get 2D canvas context");
    this.ctx = ctx;
  }

  /**
   * @param state    Reconstructed sim state at this frame.
   * @param meta     Start metadata (grid size, base map).
   * @param seatBySnakeId  Maps each sim snake_id to the runner-assigned
   *                       seat. Color comes from the seat, not the snake_id —
   *                       the sim is free to pick whatever snake_ids it
   *                       wants and we don't want the colors to drift away
   *                       from the seat-indexed labels in the legend / exec
   *                       times bar. Pass an empty map to fall back to
   *                       coloring by snake_id (legacy behavior).
   */
  render(state: SimState, meta: SimStartData, seatBySnakeId: Map<number, number>): void {
    const { width: gW, height: gH } = meta;
    const cW = this.canvas.width;
    const cH = this.canvas.height;
    const cellW = cW / gW;
    const cellH = cH / gH;
    const ctx   = this.ctx;

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
    ctx.lineWidth   = 0.5;
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
    ctx.fillStyle  = FOOD_COLOR;
    for (const key of state.food) {
      const [fx, fy] = key.split(",").map(Number);
      ctx.beginPath();
      const cx = fx * cellW + cellW / 2;
      const cy = fy * cellH + cellH / 2;
      const r  = Math.max(1, Math.min(cellW, cellH) / 2 - foodPad);
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.fill();
    }

    // Snakes — bodies first, then heads on top so the head is never obscured
    const tube    = Math.min(cellW, cellH) * TUBE_RATIO;
    const bodyPad = Math.max(1, Math.floor(Math.min(cellW, cellH) * 0.1));

    const totalSnakes = seatBySnakeId.size || state.snakes.size;
    for (const [id, snake] of state.snakes) {
      const seat      = seatBySnakeId.get(id) ?? id;
      const palette   = colorForSeat(seat, totalSnakes);
      const bodyColor = snake.alive ? palette.body : DEAD_COLOR;
      const body      = snake.body;

      ctx.fillStyle = bodyColor;
      for (let i = 1; i < body.length; i++) {
        const [gx, gy] = body[i];
        const [px, py] = body[i - 1]; // toward head

        const toPrevDx = px - gx;
        const toPrevDy = py - gy;

        let toNextDx: number, toNextDy: number;
        if (i + 1 < body.length) {
          toNextDx = body[i + 1][0] - gx;
          toNextDy = body[i + 1][1] - gy;
        } else {
          toNextDx = toPrevDx;
          toNextDy = toPrevDy;
        }

        _drawPipeSegment(
          ctx, gx, gy, cellW, cellH, tube,
          toPrevDx === -1 || toNextDx === -1, // connLeft
          toPrevDx ===  1 || toNextDx ===  1, // connRight
          toPrevDy === -1 || toNextDy === -1, // connTop
          toPrevDy ===  1 || toNextDy ===  1, // connBottom
        );
      }

      // Head — full tile minus a small inset so it stands out against the body pipe
      ctx.fillStyle = snake.alive ? palette.head : DEAD_COLOR;
      const [hx, hy] = body[0];
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

/**
 * Draws a pipe segment for one body cell.
 *
 * The pipe is built from up to four arms (one per connected neighbour) plus a
 * centre square that fills the corner gap when two perpendicular arms meet.
 * Each arm runs from the cell edge to the cell centre so adjacent segments
 * connect seamlessly with no gap.
 */
function _drawPipeSegment(
  ctx: CanvasRenderingContext2D,
  gx: number, gy: number,
  cellW: number, cellH: number,
  tube: number,
  connLeft: boolean, connRight: boolean,
  connTop: boolean, connBottom: boolean,
): void {
  const cx = gx * cellW + cellW / 2;
  const cy = gy * cellH + cellH / 2;
  const h  = tube / 2;

  // Centre square — always drawn; fills the gap where two arms meet at a corner
  ctx.fillRect(cx - h, cy - h, tube, tube);

  // Arms: each runs from the cell edge to the centre
  if (connLeft)   ctx.fillRect(gx * cellW,       cy - h,        cx - gx * cellW,           tube);
  if (connRight)  ctx.fillRect(cx,                cy - h,        (gx + 1) * cellW - cx,     tube);
  if (connTop)    ctx.fillRect(cx - h,            gy * cellH,    tube, cy - gy * cellH      );
  if (connBottom) ctx.fillRect(cx - h,            cy,            tube, (gy + 1) * cellH - cy);
}
