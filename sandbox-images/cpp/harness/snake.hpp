#pragma once
#include "types.hpp"
#include <utility>

class Snake {
public:
    Snake();
    void set_id(int id);
    void set_start_length(int length);
    void set_start_position(Coord pos);
    void set_init_data(EnvInitData data);
    std::pair<int, int> update(EnvStepData data);
private:
    int id_ = -1;
    int start_length_ = 0;
    Coord start_position_{};
    EnvInitData init_data_{};
};
