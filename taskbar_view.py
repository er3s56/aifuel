# -*- coding: utf-8 -*-
"""任务栏的紧凑排版；按内容测量列宽，保留全部额度字段。"""
from dataclasses import dataclass
import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF

import display

GAP, PAD, BAR, COLUMN_GAP = 4, 4, 24, 8


def fonts():
    return QFont("Segoe UI", 8), QFont("Consolas", 9, QFont.Bold), QFont("Segoe UI Semibold", 8)


@dataclass(frozen=True)
class Column:
    brand: int
    label: int
    value: int
    reset: int

    @property
    def width(self):
        return PAD * 2 + GAP * 4 + self.brand + self.label + self.value + BAR + self.reset


def measure(readings):
    small, value, brand = (QFontMetricsF(font) for font in fonts())
    columns = []
    for offset in range(0, max(4, len(readings)), 2):
        pair = readings[offset:offset + 2]
        # 预留三位百分比、缓存标记和最长日期；数字/状态变化不会导致窗口来回伸缩。
        columns.append(Column(
            max(22, math.ceil(brand.horizontalAdvance("GPT"))),
            max([18] + [math.ceil(small.horizontalAdvance(r.label)) + 1 for r in pair]),
            max([40, math.ceil(value.horizontalAdvance("100%⟳")) + 2]
                + [math.ceil(value.horizontalAdvance(r.text + "⟳")) + 2 for r in pair if r.text]),
            max(65, *(math.ceil(small.horizontalAdvance(text)) + 2
                      for text in ("09-30 23:59", "窗口已重置", "数据已过期", "限流等待"))),
        ))
    return columns


def width(columns):
    return sum(column.width for column in columns) + max(0, len(columns) - 1) * COLUMN_GAP


def paint(painter, rect, readings, columns, light, loading=False):
    # 保留鼠标命中区域，底色几乎全透明，直接透出系统任务栏材质。
    painter.fillRect(rect, QColor(0, 0, 0, 1))
    fg, dim, track = ("#202020", "#565d66", "#bec3c8") if light else ("#f0f0f0", "#a6abb3", "#50545b")
    colors = ({display.GREEN: "#167333", display.YELLOW: "#955600", display.RED: "#bd271b",
               display.FG: fg, display.FG_DIM: dim} if light else {display.FG: fg, display.FG_DIM: dim})
    brands = {"claude": "#984827", "chatgpt": "#087d62"} if light else display.BRAND
    small, value, brand = fonts()
    if loading:
        painter.setFont(small)
        painter.setPen(QColor(dim))
        painter.drawText(rect, Qt.AlignCenter, "读取中…")
        return
    row_height = (rect.height() - 4) / 2
    offset = 0
    for index, column in enumerate(columns):
        for row, reading in enumerate(readings[index * 2:index * 2 + 2]):
            resolved = display.resolve(reading)
            text = resolved.text if resolved.text is not None else (
                "--" if resolved.percent is None else "%d%%" % round(resolved.percent))
            y, x = 2 + row * row_height, offset + PAD

            def draw(text, span, font, color, alignment=Qt.AlignLeft):
                nonlocal x
                painter.setFont(font)
                painter.setPen(QColor(color))
                painter.drawText(QRectF(x, y, span, row_height), Qt.AlignVCenter | alignment, text)
                x += span + GAP

            draw(display.NAME.get(reading.provider, reading.provider[:3].upper()), column.brand,
                 brand, brands.get(reading.provider, fg))
            draw(reading.label, column.label, small, dim)
            color = dim if resolved.dim else colors.get(resolved.color, resolved.color)
            draw(text + resolved.note, column.value, value, color, Qt.AlignRight)
            if reading.text is None:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(track))
                bar = QRectF(x, y + row_height / 2 - 2, BAR, 4)
                painter.drawRoundedRect(bar, 2, 2)
                if resolved.percent is not None:
                    bar.setWidth(BAR * min(100, max(0, resolved.percent)) / 100)
                    painter.setBrush(QColor(color))
                    painter.drawRoundedRect(bar, 2, 2)
            x += BAR + GAP
            draw(display.status_text(reading), column.reset, small, dim, Qt.AlignRight)
        offset += column.width + COLUMN_GAP
        if index < len(columns) - 1:
            color = QColor(dim)
            color.setAlpha(55)
            painter.setPen(color)
            painter.drawLine(int(offset - COLUMN_GAP / 2), 7,
                             int(offset - COLUMN_GAP / 2), int(rect.height() - 7))
