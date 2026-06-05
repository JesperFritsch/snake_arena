// set_id(id)               — called once; your integer ID in this game
// set_start_length(n)      — called once; initial body length
// set_start_position(pos)  — called once; initial head position {x: number, y: number}
// set_init_data(data)      — called once; full environment metadata (see below)
//
// init_data fields (object):
//   height: number                          — grid height in cells
//   width: number                           — grid width in cells
//   free_value: number                      — cell value for empty space
//   blocked_value: number                   — cell value for walls
//   food_value: number                      — cell value for food
//   snake_tags: {[id]: string}              — display name per snake id
//   snake_values: {[id]: {head_value: number, body_value: number}}  — per snake id
//   start_positions: {[id]: {x: number, y: number}}                 — starting position per snake id
//   base_map: number[][]                    — static map (walls/free cells); row 0 is top
//
// update(data) — called every step; return [dx, dy] to move:
//   [1, 0] right   [-1, 0] left   [0, 1] down   [0, -1] up
//
// data fields (object):
//   map: number[][]                         — current grid (walls + snakes + food); row 0 is top
//   snakes: {[id]: {is_alive: boolean, length: number}}             — per snake id
//   food_locations: Array<{x: number, y: number}>                   — food positions

'use strict';

class Snake {
  constructor() {
    this.id = null;
    this.initData = null;
  }

  setId(id)              { this.id = id; }
  setStartLength(_n)     {}
  setStartPosition(_pos) {}
  setInitData(data)      { this.initData = data; }

  update(data) {
    console.log(`step! grid=${this.initData.height}x${this.initData.width}`
              + ` food=${data.food_locations.length}`);
    return [0, 1]; // always move down — replace with your logic
  }
}

module.exports = { Snake };
