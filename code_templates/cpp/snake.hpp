#pragma once
#include "snake_base.hpp"

class Snake : public SnakeBase {
public:
    Snake();
    void set_id(int id) override;
    void set_start_length(int n) override;
    void set_start_position(Coord pos) override;
    void set_init_data(EnvInitData data) override;
    std::pair<int, int> update(EnvStepData data) override;

private:
    int id_ = -1;
    EnvInitData init_data_{};
    // Add your own fields here
};
