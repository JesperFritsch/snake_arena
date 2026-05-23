use crate::types::{Coord, EnvInitData, EnvStepData};

pub struct Snake {
    id: Option<i32>,
    start_length: Option<i32>,
    start_position: Option<Coord>,
    init_data: Option<EnvInitData>,
}

impl Snake {
    pub fn new() -> Self {
        Self {
            id: None,
            start_length: None,
            start_position: None,
            init_data: None,
        }
    }

    /// Called once at startup. Use `id` to locate your snake on the map
    /// (your head cell value is `init_data.snake_values[id].head_value`).
    pub fn set_id(&mut self, id: i32) {
        self.id = Some(id);
    }

    /// Called once at startup with the snake's initial body length.
    pub fn set_start_length(&mut self, length: i32) {
        self.start_length = Some(length);
    }

    /// Called once at startup with the snake's initial head position.
    pub fn set_start_position(&mut self, pos: Coord) {
        self.start_position = Some(pos);
    }

    /// Called once before the first step with full environment metadata:
    /// grid dimensions, cell sentinel values, all snake tags/values, start positions.
    pub fn set_init_data(&mut self, data: EnvInitData) {
        self.init_data = Some(data);
    }

    /// Called every simulation step. Return the direction to move as `(dx, dy)`:
    ///   (1, 0)  = right
    ///   (-1, 0) = left
    ///   (0, 1)  = down
    ///   (0, -1) = up
    pub fn update(&mut self, data: EnvStepData) -> (i32, i32) {
        let init = self.init_data.as_ref().unwrap();
        println!(
            "step! grid={}x{} food={:?}",
            init.height,
            init.width,
            data.food_locations.iter().map(|c| (c.x, c.y)).collect::<Vec<_>>()
        );
        (0, 1) // always move down — replace with your logic
    }
}
