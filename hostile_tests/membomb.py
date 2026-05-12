class Snake:
    def __init__(self):
        self.blob = []
        while True:
            self.blob.append(b"x" * (10 * 1024 * 1024))

    def set_id(self, _): pass
    def set_start_length(self, _): pass
    def set_start_position(self, _): pass
    def set_init_data(self, _): pass
    def update(self, _): return (0, 1)
