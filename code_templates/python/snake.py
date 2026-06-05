# set_id(id)               — called once; your integer ID in this game
# set_start_length(n)      — called once; initial body length
# set_start_position(pos)  — called once; initial head position as tuple[int, int] (x, y)
# set_init_data(data)      — called once; full environment metadata (see below)
#
# init_data keys (dict):
#   'height': int                         — grid height in cells
#   'width': int                          — grid width in cells
#   'free_value': int                     — cell value for empty space
#   'blocked_value': int                  — cell value for walls
#   'food_value': int                     — cell value for food
#   'snake_tags': dict[int, str]          — display name per snake id
#   'snake_values': dict[int, dict]       — {'head_value': int, 'body_value': int} per snake id
#   'start_positions': dict[int, dict]    — {'x': int, 'y': int} starting head position per snake id
#   'base_map': list[list[int]]           — static map (walls/free cells); row 0 is top
#
# update(data) — called every step; return (dx, dy) to move:
#   (1, 0) right   (-1, 0) left   (0, 1) down   (0, -1) up
#
# data keys (dict):
#   'map': list[list[int]]                — current grid (walls + snakes + food); row 0 is top
#   'snakes': dict[int, dict]             — {'is_alive': bool, 'length': int} per snake id
#   'food_locations': list[dict]          — [{'x': int, 'y': int}, ...]


class Snake:
    def __init__(self):
        self.id = None
        self.init_data = None

    def set_id(self, id: int):
        self.id = id

    def set_start_length(self, length: int):
        pass

    def set_start_position(self, pos: tuple[int, int]):
        pass

    def set_init_data(self, data: dict):
        self.init_data = data

    def update(self, data: dict) -> tuple[int, int]:
        print(f"step! grid={self.init_data['height']}x{self.init_data['width']}"
              f" food={len(data['food_locations'])}")
        return (0, 1)  # always move down — replace with your logic
