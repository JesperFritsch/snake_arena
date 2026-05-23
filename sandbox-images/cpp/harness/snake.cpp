#include "snake.hpp"

Snake::Snake() = default;
void Snake::set_id(int id) { id_ = id; }
void Snake::set_start_length(int length) { start_length_ = length; }
void Snake::set_start_position(Coord pos) { start_position_ = pos; }
void Snake::set_init_data(EnvInitData data) { init_data_ = std::move(data); }
std::pair<int, int> Snake::update(EnvStepData /*data*/) { return {0, 0}; }
