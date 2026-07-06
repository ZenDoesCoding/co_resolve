import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interfaces.tui import CoResolveTUI
from utils.docker_helper import ensure_docker_running

if __name__ == "__main__":
    print("Checking Docker daemon status...")
    ensure_docker_running(interactive=True)
    tui = CoResolveTUI()
    tui.run()

