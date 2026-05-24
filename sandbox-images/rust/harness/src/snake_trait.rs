use crate::types::{Coord, EnvInitData, EnvStepData};

pub trait SnakeTrait: Send {
    fn set_id(&mut self, id: i32);
    fn set_start_length(&mut self, n: i32);
    fn set_start_position(&mut self, pos: Coord);
    fn set_init_data(&mut self, data: EnvInitData);
    fn update(&mut self, data: EnvStepData) -> (i32, i32);
}
