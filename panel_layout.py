"""Logical-pixel geometry for readable, wrapping quota panels."""
import math

CELL_WIDTH = 300
ROW_HEIGHT = 22
MIN_ITEMS = 4


def columns(mode, width, count):
    return min(max(MIN_ITEMS, count), max(1, width // CELL_WIDTH)) if mode == "horizontal" else 1


def minimum_height(mode, width, count):
    rows = math.ceil(max(MIN_ITEMS, count) / columns(mode, width, count))
    return rows * (20 if mode == "horizontal" else ROW_HEIGHT) + (8 if mode == "horizontal" else 16)


def saved_size(config, mode):
    default = (CELL_WIDTH * 2, 48) if mode == "horizontal" else (CELL_WIDTH, 104)
    sizes = config.get("sizes")
    value = sizes.get(mode) if isinstance(sizes, dict) else None
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(n) is not int or not 1 <= n <= 8000 for n in value)):
        return default
    return max(CELL_WIDTH, value[0]), value[1]


def cells(mode, width, height, count):
    cols = columns(mode, width, count)
    rows = math.ceil(max(MIN_ITEMS, count) / cols)
    padding = 4 if mode == "horizontal" else 8
    row_height = (height - padding * 2) / rows
    cell_width = width / cols
    # Read each column top to bottom, retaining related provider rows together.
    return [(i // rows * cell_width, padding + i % rows * row_height,
             cell_width, row_height) for i in range(count)]
