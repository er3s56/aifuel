"""Keep saved floating-window positions on the nearest available monitor."""
import math


def visible_position(x, y, width, height, areas):
    if not areas:
        return x, y
    if not isinstance(x, (int, float)) or not math.isfinite(x):
        x = areas[0][0]
    if not isinstance(y, (int, float)) or not math.isfinite(y):
        y = areas[0][1]
    candidates = [(max(left, min(x, left + max(0, w - width))),
                   max(top, min(y, top + max(0, h - height))))
                  for left, top, w, h in areas]
    px, py = min(candidates, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)
    return int(px), int(py)
