# -*- coding: utf-8 -*-
"""生成 aifuel.ico —— 两根油量条，直接呼应窗口本身的样子。

小尺寸下细节全糊，所以只留最能辨认的元素：深色圆角底 + 一橙一绿两根条。
橙=Claude，绿=ChatGPT，和窗口里的品牌色一致。
"""
import os

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

BG = "#14161a"
TRACK = "#3a3f47"
CLAUDE = "#d97757"
CHATGPT = "#10a37f"

SIZES = [256, 128, 64, 48, 32, 16]


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)

    s = size / 256.0                      # 所有尺寸按 256 基准等比缩放

    bg = QPainterPath()
    bg.addRoundedRect(QRectF(0, 0, size, size), 56 * s, 56 * s)
    p.fillPath(bg, QColor(BG))

    bar_h = 40 * s
    x0, x1 = 46 * s, 210 * s
    for i, (color, frac) in enumerate(((CLAUDE, 0.62), (CHATGPT, 0.30))):
        y = (92 + i * 62) * s - bar_h / 2
        track = QPainterPath()
        track.addRoundedRect(QRectF(x0, y, x1 - x0, bar_h), bar_h / 2, bar_h / 2)
        p.fillPath(track, QColor(TRACK))
        fill = QPainterPath()
        fill.addRoundedRect(QRectF(x0, y, (x1 - x0) * frac, bar_h), bar_h / 2, bar_h / 2)
        p.fillPath(fill, QColor(color))

    p.end()
    return img


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    # Qt 的 ICO 写入器只吃单张图，多尺寸得自己塞进容器 —— 用 Pillow 更省事，
    # 没有 Pillow 就退回单张 256（Windows 会自己降采样，小图标略糊但可用）。
    imgs = {s: render(s) for s in SIZES}
    out = os.path.join(here, "aifuel.ico")
    try:
        from PIL import Image
        pngs = []
        for s in SIZES:
            path = os.path.join(here, "_icon_%d.png" % s)
            imgs[s].save(path, "PNG")
            pngs.append(path)
        base = Image.open(pngs[0])
        base.save(out, format="ICO", sizes=[(s, s) for s in SIZES])
        for path in pngs:
            os.remove(path)
        print("已用 Pillow 写入多尺寸 ico:", SIZES)
    except ImportError:
        imgs[256].save(out, "ICO")
        print("没有 Pillow，写入单尺寸 256 ico")
    print("->", out, os.path.getsize(out), "bytes")
    imgs[256].save(os.path.join(here, "icon_preview.png"), "PNG")
