#include "snake.hpp"
#include <iostream>

// EnvInitData fields (set once before the first step):
//   height, width          — grid dimensions
//   free_value             — cell value for empty space
//   blocked_value          — cell value for walls
//   food_value             — cell value for food
//   snake_tags[id]         — name string for each snake
//   snake_values[id]       — {head_value, body_value} for each snake
//   start_positions[id]    — {x, y} starting head position for each snake
//   base_map[row][col]     — static map (walls/free cells); row 0 is top
//   base_map_dtype         — numpy dtype string for the raw bytes
//
// EnvStepData fields (passed every step):
//   map[row][col]          — full current grid (walls + snakes + food)
//   snakes[id]             — {is_alive, length} for each snake
//   food_locations         — list of {x, y} food positions
//
// update() must return {dx, dy}:
//   { 1,  0} = right   {-1,  0} = left
//   { 0,  1} = down    { 0, -1} = up

Snake::Snake() = default;

void Snake::set_id(int id) {
    id_ = id;
}

void Snake::set_start_length(int length) {
    start_length_ = length;
}

void Snake::set_start_position(Coord pos) {
    start_position_ = pos;
}

void Snake::set_init_data(EnvInitData data) {
    init_data_ = std::move(data);
}

std::pair<int, int> Snake::update(EnvStepData data) {
    std::cout << "step! grid=" << init_data_.height << "x" << init_data_.width
              << " food=" << data.food_locations.size() << "\n";

    return {0, 1}; // always move down — replace with your logic
}
