"""生成应用图标。

使用现有的 QPainterPath 矢量图标系统 + Pillow 合成图标。
ICO 输出到 assets/app.ico（256 单帧）；_SIZES 六档分辨率仅用于导出 PNG。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QPixmap, QImage
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PIL import Image

from pc_api.gui.icons import Icon
from pc_api.gui.styles import LightPalette


# 六档分辨率仅用于导出 PNG（ICO 为 256 单帧）
_SIZES = [16, 32, 48, 64, 128, 256]


def _qimage_to_pil(qimg: QImage) -> Image.Image:
    """QImage -> PIL Image（RGBA8888）。"""
    ptr = qimg.bits()
    raw = bytes(memoryview(ptr))
    return Image.frombytes(
        "RGBA", (qimg.width(), qimg.height()), raw, "raw", "RGBA", 0, 1
    )


def render_icon(name: str, color: str, sizes: list[int]) -> dict[int, Image.Image]:
    """渲染图标为多分辨率 PIL Image 字典。"""
    results = {}
    for size in sizes:
        pm = Icon(name, color).to_pixmap(size, size)
        qimg = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        results[size] = _qimage_to_pil(qimg)
    return results


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    palette = LightPalette()
    accent = palette.PRIMARY

    out_dir = Path(__file__).parent / "assets"
    out_dir.mkdir(exist_ok=True)

    # 1. 主图标：麦克风（带实心背景圆形）
    print("渲染应用图标（麦克风）...")
    icons = render_icon("mic", accent, [256])

    # 用最大尺寸生成带背景的版本
    base = icons[256].convert("RGBA")
    bg = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(bg)
    draw.ellipse([8, 8, 248, 248], fill=(13, 148, 136, 255))
    base_on_bg = Image.alpha_composite(bg, base)

    # 保存 ICO：256px 单帧（Windows/Qt 自动缩放）
    ico_path = out_dir / "app.ico"
    base_on_bg.save(ico_path, format="ICO", sizes=[(256, 256)])
    print(f"  -> {ico_path} (256px)")

    # 额外保存各尺寸 PNG
    for size in _SIZES:
        png_path = out_dir / f"app_{size}.png"
        if size == 256:
            img = base_on_bg
        else:
            img = base_on_bg.resize((size, size), Image.LANCZOS)
        img.save(png_path)
    print(f"  -> app_*.png ({len(_SIZES)} 个分辨率)")

    # 2. 录音图标（实心红圆）
    print("渲染录音图标...")
    record_pm = Icon("record", "#EF4444").to_pixmap(256, 256)
    record_qimg = record_pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    record_base = _qimage_to_pil(record_qimg)

    record_ico = out_dir / "record.ico"
    record_base.save(record_ico, format="ICO", sizes=[(256, 256)])
    print(f"  -> {record_ico}")

    # 3. 小尺寸 PNG（用于任务栏/桌面）
    for name, color, fname in [
        ("mic", accent, "app"),
        ("record", "#EF4444", "record"),
        ("play", accent, "play"),
        ("settings", palette.TEXT_SECONDARY, "settings"),
    ]:
        pm = Icon(name, color).to_pixmap(128, 128)
        qimg = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        pil_img = _qimage_to_pil(qimg)
        png_path = out_dir / f"{fname}.png"
        pil_img.save(png_path)
        print(f"  -> {png_path}")

    print("\n完成！所有图标已保存到 assets/")


if __name__ == "__main__":
    main()