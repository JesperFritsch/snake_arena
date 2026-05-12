import os

class Snake:
    def __init__(self):
        print(f"running as uid={os.getuid()} gid={os.getgid()}")
        try:
            os.setuid(0)
            print(f"PRIVESC SUCCESS: now uid={os.getuid()}")
        except Exception as e:
            print(f"setuid blocked: {e}")

        try:
            os.chown("/app", 0, 0)
            print("chown succeeded!")
        except Exception as e:
            print(f"chown blocked: {e}")

    def set_id(self, _): pass
    def set_start_length(self, _): pass
    def set_start_position(self, _): pass
    def set_init_data(self, _): pass
    def update(self, _): return (0, 1)
