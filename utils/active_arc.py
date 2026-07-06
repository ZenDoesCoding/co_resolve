import os
import json

ACTIVE_ARC_FILE = ".active_arc"

def get_active_arc() -> int:
    if os.path.exists(ACTIVE_ARC_FILE):
        try:
            with open(ACTIVE_ARC_FILE, "r") as f:
                data = json.load(f)
                arc = int(data.get("active_arc", 2))
                if arc in (1, 2, 3):
                    return arc
        except Exception:
            pass
    return 2

def set_active_arc(arc_num: int) -> bool:
    if arc_num not in (1, 2, 3):
        return False
    try:
        with open(ACTIVE_ARC_FILE, "w") as f:
            json.dump({"active_arc": arc_num}, f)
        return True
    except Exception:
        return False
