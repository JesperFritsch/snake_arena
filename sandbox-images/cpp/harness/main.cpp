#include <cstring>
#include <iostream>
#include <mutex>
#include <string>
#include <vector>

#include <grpcpp/grpcpp.h>

#include "sim_interface.grpc.pb.h"
#include "sim_interface.pb.h"
#include "snake.hpp"
#include "types.hpp"

// Proto message aliases to avoid ambiguity with identically-named user types.
using ProtoEnvInitData = snake_sim::EnvInitData;
using ProtoEnvData     = snake_sim::EnvData;

static std::vector<std::vector<int>> bytes_to_grid(
    const std::string& data, int height, int width, const std::string& dtype)
{
    size_t n = static_cast<size_t>(height) * width;
    std::vector<int> flat;
    flat.reserve(n);

    auto push_le32 = [&](size_t i) {
        int32_t v; std::memcpy(&v, data.data() + i, 4); flat.push_back(v);
    };
    auto push_le64 = [&](size_t i) {
        int64_t v; std::memcpy(&v, data.data() + i, 8);
        flat.push_back(static_cast<int>(v));
    };
    auto push_f32 = [&](size_t i) {
        float v; std::memcpy(&v, data.data() + i, 4);
        flat.push_back(static_cast<int>(v));
    };

    if (dtype == "int64" || dtype == "<i8" || dtype == ">i8") {
        for (size_t i = 0; i + 8 <= data.size(); i += 8) push_le64(i);
    } else if (dtype == "float32" || dtype == "<f4" || dtype == ">f4") {
        for (size_t i = 0; i + 4 <= data.size(); i += 4) push_f32(i);
    } else {
        for (size_t i = 0; i + 4 <= data.size(); i += 4) push_le32(i);
    }
    flat.resize(n, 0);

    std::vector<std::vector<int>> grid(height, std::vector<int>(width));
    for (int r = 0; r < height; r++)
        for (int c = 0; c < width; c++)
            grid[r][c] = flat[static_cast<size_t>(r) * width + c];
    return grid;
}

class SnakeServiceImpl final : public snake_sim::RemoteSnake::Service {
    std::mutex mu_;
    Snake snake_;
    int height_ = 0, width_ = 0;
    std::string dtype_;

public:
    grpc::Status SetId(grpc::ServerContext*, const snake_sim::SnakeId* req,
                       snake_sim::Empty*) override
    {
        std::lock_guard<std::mutex> lock(mu_);
        snake_.set_id(req->id());
        return grpc::Status::OK;
    }

    grpc::Status SetStartLength(grpc::ServerContext*, const snake_sim::StartLength* req,
                                snake_sim::Empty*) override
    {
        std::lock_guard<std::mutex> lock(mu_);
        snake_.set_start_length(req->length());
        return grpc::Status::OK;
    }

    grpc::Status SetStartPosition(grpc::ServerContext*, const snake_sim::StartPosition* req,
                                  snake_sim::Empty*) override
    {
        std::lock_guard<std::mutex> lock(mu_);
        const auto& c = req->start_position();
        snake_.set_start_position({c.x(), c.y()});
        return grpc::Status::OK;
    }

    grpc::Status SetInitData(grpc::ServerContext*, const ProtoEnvInitData* req,
                             snake_sim::Empty*) override
    {
        std::lock_guard<std::mutex> lock(mu_);
        height_ = req->height();
        width_  = req->width();
        dtype_  = req->base_map_dtype();

        EnvInitData init;
        init.height        = height_;
        init.width         = width_;
        init.free_value    = req->free_value();
        init.blocked_value = req->blocked_value();
        init.food_value    = req->food_value();
        init.base_map_dtype = dtype_;

        for (const auto& [k, v] : req->snake_tags())
            init.snake_tags[k] = v;
        for (const auto& [k, v] : req->snake_values())
            init.snake_values[k] = {v.head_value(), v.body_value()};
        for (const auto& [k, c] : req->start_positions())
            init.start_positions[k] = {c.x(), c.y()};

        init.base_map = bytes_to_grid(req->base_map(), height_, width_, dtype_);
        snake_.set_init_data(std::move(init));
        return grpc::Status::OK;
    }

    grpc::Status Update(grpc::ServerContext*,
                        grpc::ServerReaderWriter<snake_sim::UpdateResponse,
                                                 ProtoEnvData>* stream) override
    {
        ProtoEnvData env;
        while (stream->Read(&env)) {
            std::pair<int,int> dir;
            {
                std::lock_guard<std::mutex> lock(mu_);
                EnvStepData step;
                step.map = bytes_to_grid(env.map(), height_, width_, dtype_);
                for (const auto& [k, s] : env.snakes())
                    step.snakes[k] = {s.is_alive(), s.length()};
                for (const auto& c : env.food_locations())
                    step.food_locations.push_back({c.x(), c.y()});
                dir = snake_.update(std::move(step));
            }
            std::cout << "---STEP_END---\n" << std::flush;
            snake_sim::UpdateResponse resp;
            resp.mutable_direction()->set_x(dir.first);
            resp.mutable_direction()->set_y(dir.second);
            stream->Write(resp);
        }
        return grpc::Status::OK;
    }

    grpc::Status Reset(grpc::ServerContext*, const snake_sim::Empty*,
                       snake_sim::Empty*) override
    { return grpc::Status::OK; }

    grpc::Status Kill(grpc::ServerContext*, const snake_sim::Empty*,
                      snake_sim::Empty*) override
    { return grpc::Status::OK; }
};

int main() {
    SnakeServiceImpl service;
    grpc::ServerBuilder builder;
    builder.AddListeningPort("0.0.0.0:50051", grpc::InsecureServerCredentials());
    builder.RegisterService(&service);
    auto server = builder.BuildAndStart();
    server->Wait();
    return 0;
}
