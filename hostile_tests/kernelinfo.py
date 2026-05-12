import os

class Snake:
    def __init__(self):
        try:
            with open("/proc/version") as f:
                print(f"kernel: {f.read().strip()}")
        except Exception as e:
            print(f"/proc/version: {e}")
        print(f"uname: {os.uname()}")

    def set_id(self, _): pass
    def set_start_length(self, _): pass
    def set_start_position(self, _): pass
    def set_init_data(self, _): pass
    def update(self, _): return (0, 1)
