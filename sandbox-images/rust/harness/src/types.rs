use std::collections::HashMap;

#[derive(Debug, Clone)]
pub struct Coord {
    pub x: i32,
    pub y: i32,
}

#[derive(Debug, Clone)]
pub struct SnakeValues {
    pub head_value: i32,
    pub body_value: i32,
}

#[derive(Debug, Clone)]
pub struct SnakeRep {
    pub is_alive: bool,
    pub length: i32,
}

#[derive(Debug, Clone)]
pub struct EnvInitData {
    pub height: i32,
    pub width: i32,
    pub free_value: i32,
    pub blocked_value: i32,
    pub food_value: i32,
    pub snake_tags: HashMap<i32, String>,
    pub snake_values: HashMap<i32, SnakeValues>,
    pub start_positions: HashMap<i32, Coord>,
    /// The game map grid decoded from raw bytes; rows × cols of cell values.
    pub base_map: Vec<Vec<i32>>,
    pub base_map_dtype: String,
}

#[derive(Debug, Clone)]
pub struct EnvStepData {
    /// Current map state; rows × cols of cell values.
    pub map: Vec<Vec<i32>>,
    pub snakes: HashMap<i32, SnakeRep>,
    pub food_locations: Vec<Coord>,
}
