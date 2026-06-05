// set_id(id)              — called once; your integer ID in this game
// set_start_length(n)     — called once; initial body length
// set_start_position(pos) — called once; initial head position Coord { x: i32, y: i32 }
// set_init_data(data)     — called once; full environment metadata (see below)
//
// EnvInitData fields:
//   height, width          — i32: grid dimensions
//   free_value             — i32: cell value for empty space
//   blocked_value          — i32: cell value for walls
//   food_value             — i32: cell value for food
//   snake_tags             — HashMap<i32, String>: display name per snake id
//   snake_values           — HashMap<i32, SnakeValues>: { head_value: i32, body_value: i32 } per snake id
//   start_positions        — HashMap<i32, Coord>: { x: i32, y: i32 } starting position per snake id
//   base_map               — Vec<Vec<i32>>: static map (walls/free cells); row 0 is top
//
// update(&mut self, data) — called every step; return (dx, dy) to move:
//   (1, 0) right   (-1, 0) left   (0, 1) down   (0, -1) up
//
// EnvStepData fields:
//   map                    — Vec<Vec<i32>>: current grid (walls + snakes + food); row 0 is top
//   snakes                 — HashMap<i32, SnakeRep>: { is_alive: bool, length: i32 } per snake id
//   food_locations         — Vec<Coord>: food positions { x: i32, y: i32 }

// Harness types (from types.rs, not editable):
//   struct Coord       { pub x: i32, pub y: i32 }
//   struct SnakeValues { pub head_value: i32, pub body_value: i32 }
//   struct SnakeRep    { pub is_alive: bool, pub length: i32 }
//   EnvInitData / EnvStepData fields documented above

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
