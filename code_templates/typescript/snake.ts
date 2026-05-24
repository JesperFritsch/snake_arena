import type { Coord, EnvInitData, EnvStepData } from '../types';

// setInitData receives an object with:
//   height, width          — grid dimensions
//   free_value             — cell value for empty space
//   blocked_value          — cell value for walls
//   food_value             — cell value for food
//   snake_tags[id]         — name string for each snake (keys are numbers)
//   snake_values[id]       — { head_value, body_value } for each snake
//   start_positions[id]    — { x, y } starting head position for each snake
//   base_map[row][col]     — static map (walls/free cells); row 0 is top
//   base_map_dtype         — numpy dtype string for the raw bytes
//
// update receives an object with:
//   map[row][col]          — full current grid (walls + snakes + food)
//   snakes[id]             — { is_alive, length } for each snake
//   food_locations         — array of { x, y } food positions
//
// update must return [dx, dy]:
//   [ 1,  0] = right   [-1,  0] = left
//   [ 0,  1] = down    [ 0, -1] = up

export class Snake {
  private id: number | null = null;
  private initData: EnvInitData | null = null;

  setId(id: number): void { this.id = id; }
  setStartLength(_length: number): void {}
  setStartPosition(_pos: Coord): void {}
  setInitData(data: EnvInitData): void { this.initData = data; }

  update(data: EnvStepData): [number, number] {
    console.log(
      `step! grid=${this.initData!.height}x${this.initData!.width}` +
      ` food=${data.food_locations.length}`
    );
    return [0, 1]; // always move down — replace with your logic
  }
}
