import os

os.environ["GRPC_VERBOSITY"] = "ERROR"

import grpc
import time
import numpy as np

from concurrent import futures
from google.protobuf.json_format import MessageToDict

from harness.usercode.snake import Snake
# This is provided by the user
from harness.stubs import sim_interface_pb2
from harness.stubs import sim_interface_pb2_grpc

def env_init_to_dict(msg):
    d = MessageToDict(msg, preserving_proto_field_name=True)
    if msg.base_map:
        d["base_map"] = np.frombuffer(msg.base_map, dtype=msg.base_map_dtype).reshape(msg.height, msg.width)
    return d

def env_data_to_dict(msg, height, width, dtype):
    return {
        "map": np.frombuffer(msg.map, dtype=dtype).reshape(height, width),
        "snakes": {sid: {"is_alive": s.is_alive, "length": s.length} for sid, s in msg.snakes.items()},
        "food_locations": [(c.x, c.y) for c in msg.food_locations],
    }

class RemoteSnakeServicer(sim_interface_pb2_grpc.RemoteSnakeServicer):
    def __init__(self, snake_instance: Snake):
        self._snake_instance = snake_instance
        self._dims = None
        self._dtype = None

    def SetId(self, request, context):
        self._snake_instance.set_id(request.id)
        return sim_interface_pb2.Empty()

    def SetStartLength(self, request, context):
        self._snake_instance.set_start_length(request.length)
        return sim_interface_pb2.Empty()

    def SetStartPosition(self, request, context):
        self._snake_instance.set_start_position((request.start_position.x, request.start_position.y))
        return sim_interface_pb2.Empty()

    def SetInitData(self, request, context):
        self._dims = (request.height, request.width)
        self._dtype = request.base_map_dtype
        self._snake_instance.set_init_data(env_init_to_dict(request))
        return sim_interface_pb2.Empty()

    def Update(self, request_iterator, context):
        for env_step_data_proto in request_iterator:
            direction = self._snake_instance.update(env_data_to_dict(env_step_data_proto, self._dims[0], self._dims[1], self._dtype))
            if direction is None:
                yield sim_interface_pb2.UpdateResponse()
            else:
                yield sim_interface_pb2.UpdateResponse(direction=sim_interface_pb2.Coord(x=direction[0], y=direction[1]))
    
    def Kill(self, request, context):
        # We dont need to do anything special to kill the snake
        # the environment will stop sending to killed snakes
        return sim_interface_pb2.Empty()

    def Reset(self, request, context):
        # Not relevant
        return sim_interface_pb2.Empty()


def serve():
    try:
        snake_instance = Snake()
        snake_servicer = RemoteSnakeServicer(snake_instance)
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        sim_interface_pb2_grpc.add_RemoteSnakeServicer_to_server(snake_servicer, server)
        server.add_insecure_port("0.0.0.0:50051")
        server.start()
        server.wait_for_termination()
    finally:
        server.stop(0)


if __name__ == '__main__':
    serve()
