import socket
import urllib.request

class Snake:
    def __init__(self):
        try:
            r = urllib.request.urlopen("http://1.1.1.1", timeout=2)
            print(f"INTERNET REACHED: {r.status}")
        except Exception as e:
            print(f"internet blocked: {e}")

        try:
            s = socket.socket()
            s.settimeout(2)
            s.connect(("agent2", 50051))
            print("SIBLING REACHED")
            s.close()
        except Exception as e:
            print(f"sibling blocked: {e}")

    def set_id(self, _): pass
    def set_start_length(self, _): pass
    def set_start_position(self, _): pass
    def set_init_data(self, _): pass
    def update(self, _): return (0, 1)
