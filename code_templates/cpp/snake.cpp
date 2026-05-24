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
// update(data) — called every step; return {dx, dy} to move:
//   { 1,  0} right   {-1,  0} left   { 0,  1} down   { 0, -1} up
//
// EnvStepData fields:
//   map[row][col]          — current grid (walls + snakes + food)
//   snakes[id]             — { is_alive, length }
//   food_locations         — vector of { x, y }

#include "snake.hpp"
#include <iostream>

Snake::Snake() = default;

void Snake::set_id(int id)                  { id_ = id; }
void Snake::set_start_length(int)           {}
void Snake::set_start_position(Coord)       {}
void Snake::set_init_data(EnvInitData data) { init_data_ = std::move(data); }

std::pair<int, int> Snake::update(EnvStepData data) {
    std::cout << "step! grid=" << init_data_.height << "x" << init_data_.width
              << " food=" << data.food_locations.size() << "\n";
    return {0, 1}; // always move down — replace with your logic
}

SnakeBase* SnakeBase::create() { return new Snake(); }
