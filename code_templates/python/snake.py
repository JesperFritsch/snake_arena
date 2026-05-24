# set_id(id)               — called once; your integer ID in this game
# set_start_length(n)      — called once; initial body length
# set_start_position(pos)  — called once; initial head position as (x, y)
# set_init_data(data)      — called once; full environment metadata (see below)
#
# init_data keys:
#   height, width          — grid dimensions
#   free_value             — cell value for empty space
#   blocked_value          — cell value for walls
#   food_value             — cell value for food
#   snake_tags[id]         — display name for each snake
#   snake_values[id]       — {'head_value': int, 'body_value': int}
#   start_positions[id]    — {'x': int, 'y': int} starting head position
#   base_map[row][col]     — static map (walls/free cells); row 0 is top
#
# update(data) — called every step; return (dx, dy) to move:
#   (1, 0) right   (-1, 0) left   (0, 1) down   (0, -1) up
#
# data keys:
#   map[row][col]          — current grid (walls + snakes + food)
#   snakes[id]             — {'is_alive': bool, 'length': int}
#   food_locations         — list of {'x': int, 'y': int}


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
