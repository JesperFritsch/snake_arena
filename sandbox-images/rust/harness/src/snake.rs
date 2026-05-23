use crate::types::{Coord, EnvInitData, EnvStepData};

pub struct Snake;

impl Snake {
    pub fn new() -> Self {
        Snake
    }

    pub fn set_id(&mut self, _id: i32) {}

    pub fn set_start_length(&mut self, _length: i32) {}

    pub fn set_start_position(&mut self, _pos: Coord) {}

    pub fn set_init_data(&mut self, _data: EnvInitData) {}

    pub fn update(&mut self, _data: EnvStepData) -> (i32, i32) {
        (0,0)
    }
}
