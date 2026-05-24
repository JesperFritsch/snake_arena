use crate::snake_trait::SnakeTrait;
use crate::types::{Coord, EnvInitData, EnvStepData};

pub struct Snake;

impl SnakeTrait for Snake {
    fn set_id(&mut self, _id: i32) {}
    fn set_start_length(&mut self, _n: i32) {}
    fn set_start_position(&mut self, _pos: Coord) {}
    fn set_init_data(&mut self, _data: EnvInitData) {}
    fn update(&mut self, _data: EnvStepData) -> (i32, i32) { (0, 0) }
}

pub fn new_snake() -> Box<dyn SnakeTrait> { Box::new(Snake) }
