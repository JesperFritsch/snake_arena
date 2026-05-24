#pragma once
#include "types.hpp"
#include <utility>

// Abstract interface compiled into main.o. Never include snake.hpp from here.
class SnakeBase {
public:
    virtual ~SnakeBase() = default;
    virtual void set_id(int id) = 0;
    virtual void set_start_length(int n) = 0;
    virtual void set_start_position(Coord pos) = 0;
    virtual void set_init_data(EnvInitData data) = 0;
    virtual std::pair<int, int> update(EnvStepData data) = 0;

    // Implemented in snake.cpp — lets main.o stay independent of Snake's layout.
    static SnakeBase* create();
};
