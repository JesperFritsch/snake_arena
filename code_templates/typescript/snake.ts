// setId(id)              — called once; your integer ID in this game
// setStartLength(n)      — called once; initial body length
// setStartPosition(pos)  — called once; initial head position {x, y}
// setInitData(data)      — called once; full environment metadata (see below)
//
// initData fields:
//   height, width          — grid dimensions
//   free_value             — cell value for empty space
//   blocked_value          — cell value for walls
//   food_value             — cell value for food
//   snake_tags[id]         — display name for each snake
//   snake_values[id]       — { head_value, body_value }
//   start_positions[id]    — { x, y } starting head position
//   base_map[row][col]     — static map (walls/free cells); row 0 is top
//
// update(data) — called every step; return [dx, dy] to move:
//   [1, 0] right   [-1, 0] left   [0, 1] down   [0, -1] up
//
// data fields:
//   map[row][col]          — current grid (walls + snakes + food)
//   snakes[id]             — { is_alive, length }
//   food_locations         — array of { x, y }

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
