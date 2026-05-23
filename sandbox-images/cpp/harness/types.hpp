#pragma once
#include <string>
#include <unordered_map>
#include <vector>

struct Coord { int x, y; };
struct SnakeValues { int head_value, body_value; };
struct SnakeRep { bool is_alive; int length; };

struct EnvInitData {
    int height, width;
    int free_value, blocked_value, food_value;
    std::unordered_map<int, std::string> snake_tags;
    std::unordered_map<int, SnakeValues> snake_values;
    std::unordered_map<int, Coord> start_positions;
    std::vector<std::vector<int>> base_map;
    std::string base_map_dtype;
};

struct EnvStepData {
    std::vector<std::vector<int>> map;
    std::unordered_map<int, SnakeRep> snakes;
    std::vector<Coord> food_locations;
};
