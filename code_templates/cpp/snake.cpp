// set_id(id)              — called once; your integer ID in this game
// set_start_length(n)     — called once; initial body length
// set_start_position(pos) — called once; initial head position Coord{ int x, int y }
// set_init_data(data)     — called once; full environment metadata (see below)
//
// EnvInitData fields:
//   height, width          — int: grid dimensions
//   free_value             — int: cell value for empty space
//   blocked_value          — int: cell value for walls
//   food_value             — int: cell value for food
//   snake_tags             — unordered_map<int, string>: display name per snake id
//   snake_values           — unordered_map<int, SnakeValues>: { int head_value, int body_value } per snake id
//   start_positions        — unordered_map<int, Coord>: { int x, int y } starting head position per snake id
//   base_map               — vector<vector<int>>: static map (walls/free cells); row 0 is top
//
// update(data) — called every step; return {dx, dy} to move:
//   { 1,  0} right   {-1,  0} left   { 0,  1} down   { 0, -1} up
//
// EnvStepData fields:
//   map                    — vector<vector<int>>: current grid (walls + snakes + food); row 0 is top
//   snakes                 — unordered_map<int, SnakeRep>: { bool is_alive, int length } per snake id
//   food_locations         — vector<Coord>: food positions { int x, int y }

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
