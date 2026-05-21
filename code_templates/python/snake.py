
import pprint


class Snake:
    def __init__(self):
        # Initialize any necessary variables or state for your snake here
        self.id = None
        self.start_length = None
        self.start_position = None
        self.init_data = None

    def set_id(self, id: int):
        # sim environment calls this method to set the snake's ID, which you can use to identify your snake in the environment
        self.id = id

    def set_start_length(self, length: int):
        # sim environment calls this method to set the snake's starting length, 
        # which you can use to initialize any necessary state for your snake
        self.start_length = length

    def set_start_position(self, start_coord: tuple[int, int]):
        # sim environment calls this method to set the snake's starting position, 
        # which you can use to initialize any necessary state for your snake
        self.start_position = start_coord

    def set_init_data(self, init_data: dict):
        # sim environment calls this method to provide your snake with necessary metadata about the environment, 
        # which you can use to initialize any necessary state for your snake
        self.init_data = init_data

    def update(self, env_step_data: dict) -> tuple[int, int] | None:
        # sim environment calls this method on every step of the simulation, 
        # providing your snake with necessary data about the current state of the environment, 
        # which you can use to decide which direction to move in
        # return a tuple representing the direction you want to move in 
        # (e.g. (0, -1) for up, (0, 1) for down, (-1, 0) for left, (1, 0) for right), or return None to not move
        print("My ID: ", self.id)
        pprint.pprint(self.init_data)
        pprint.pprint(env_step_data)  # Example of using the provided environment data
        return (0, 1)  # example: always move down