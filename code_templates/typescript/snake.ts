// setId(id)              — called once; your integer ID in this game
// setStartLength(n)      — called once; initial body length
// setStartPosition(pos)  — called once; initial head position Coord: { x: number, y: number }
// setInitData(data)      — called once; full environment metadata (see below)
//
// EnvInitData fields:
//   height: number                         — grid height in cells
//   width: number                          — grid width in cells
//   free_value: number                     — cell value for empty space
//   blocked_value: number                  — cell value for walls
//   food_value: number                     — cell value for food
//   snake_tags: Record<number, string>     — display name per snake id
//   snake_values: Record<number, SnakeValues>  — { head_value: number, body_value: number } per snake id
//   start_positions: Record<number, Coord>     — { x: number, y: number } starting position per snake id
//   base_map: number[][]                   — static map (walls/free cells); row 0 is top
//
// update(data) — called every step; return [dx, dy] to move:
//   [1, 0] right   [-1, 0] left   [0, 1] down   [0, -1] up
//
// EnvStepData fields:
//   map: number[][]                        — current grid (walls + snakes + food); row 0 is top
//   snakes: Record<number, SnakeRep>       — { is_alive: boolean, length: number } per snake id
//   food_locations: Coord[]                — food positions [{ x: number, y: number }, ...]

// Harness types (from types.ts, not editable):
//   Coord:       { x: number; y: number }
//   SnakeValues: { head_value: number; body_value: number }
//   SnakeRep:    { is_alive: boolean; length: number }
//   EnvInitData / EnvStepData fields documented above

import type { Coord, EnvInitData, EnvStepData, SnakeInterface } from '../types';

class Snake implements SnakeInterface {
  private id: number | null = null;
  private initData: EnvInitData | null = null;

  setId(id: number): void              { this.id = id; }
  setStartLength(_n: number): void     {}
  setStartPosition(_pos: Coord): void  {}
  setInitData(data: EnvInitData): void { this.initData = data; }

  update(data: EnvStepData): [number, number] {
    console.log(`step! grid=${this.initData!.height}x${this.initData!.width}`
              + ` food=${data.food_locations.length}`);
    return [0, 1]; // always move down — replace with your logic
  }
}

export function createSnake(): SnakeInterface { return new Snake(); }
