// set_id(id)              — called once; your integer ID in this game
// set_start_length(n)     — called once; initial body length
// set_start_position(pos) — called once; initial head position {x, y}
// set_init_data(data)     — called once; full environment metadata (see below)
//
// EnvInitData fields:
//   height, width          — grid dimensions
//   free_value             — cell value for empty space
//   blocked_value          — cell value for walls
//   food_value             — cell value for food
//   snake_tags[id]         — display name for each snake
//   snake_values[id]       — { head_value, body_value }
//   start_positions[id]    — { x, y } starting head position
//   base_map[row][col]     — static map (walls/free cells); row 0 is top
//
// update(&mut self, data) — called every step; return (dx, dy) to move:
//   (1, 0) right   (-1, 0) left   (0, 1) down   (0, -1) up
//
// EnvStepData fields:
//   map[row][col]          — current grid (walls + snakes + food)
//   snakes[id]             — { is_alive, length }
//   food_locations         — Vec<Coord> food positions

use crate::snake_trait::SnakeTrait;
use crate::types::{Coord, EnvInitData, EnvStepData};

pub struct Snake {
    id: Option<i32>,
    init_data: Option<EnvInitData>,
}

impl Snake {
    fn new() -> Self {
        Self { id: None, init_data: None }
    }
}

impl SnakeTrait for Snake {
    fn set_id(&mut self, id: i32)                  { self.id = Some(id); }
    fn set_start_length(&mut self, _n: i32)        {}
    fn set_start_position(&mut self, _pos: Coord)  {}
    fn set_init_data(&mut self, data: EnvInitData) { self.init_data = Some(data); }

    fn update(&mut self, data: EnvStepData) -> (i32, i32) {
        let d = self.init_data.as_ref().unwrap();
        println!("step! grid={}x{} food={}", d.height, d.width, data.food_locations.len());
        (0, 1) // always move down — replace with your logic
    }
}

pub fn new_snake() -> Box<dyn SnakeTrait> { Box::new(Snake::new()) }
