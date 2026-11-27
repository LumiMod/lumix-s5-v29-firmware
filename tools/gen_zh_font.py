#!/usr/bin/env python3
# RULES-ACTIVE-GLOBAL
"""Generate compact 1-bit CJK (22x22) + ASCII (12x22) bitmap font header cc_font_zh.h
for the S5 Control Center OpenVG OSD overlay."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT_H = Path("/Users/lixiuqi/相机功能验证/s5_v29_patched/cc_font_zh.h")
FONT_PATH = "/System/Library/Fonts/STHeiti Medium.ttc"
FONT_SIZE = 20
CELL_H = 22
CJK_W = 22
ASCII_W = 12
THRESHOLD = 112

UI_STRINGS = [
    "S5 自定义控制中心",
    "V2.9 增强版",
    "入口: 设置 > 控制中心",
    "[SD卡已保存]",
    "[SD卡已加载]",
    "[未保存]",
    "[无SD卡]",
    "SD卡LUT:",
    "个",
    "S5",
    "菜单",
    "实时 LUT 槽位",
    "LUT 快捷键",
    "Fn: 实时选LUT",
    "Fn: 打开面板",
    "关闭 (原厂)",
    "实时 LUT",
    "左右:实时切换  上下:+-10  SET/Q:确认退出",
    "LUT 生效格调",
    "对焦提示音静音",
    "菜单主题配色",
    "恢复默认设置",
    "保存配置并退出",
    "关闭 (纯V-Log)",
    "机内 1 (LUT1)",
    "机内 2 (LUT2)",
    "机内 3 (LUT3)",
    "机内 4 (LUT4)",
    "SD加载失败",
    "仅V-Log/自定",
    "全部照片格调",
    "关闭",
    "开启",
    "青蓝",
    "琥珀",
    "翠绿",
    "[重置]",
    "[保存退出]",
    "上下:选择  左右:切换  SET:+10/确认  Q/回放:退出",
]


def rasterize_glyph(font: ImageFont.FreeTypeFont, ch: str, w: int, h: int) -> list[int]:
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), ch, font=font)
    gw = bbox[2] - bbox[0]
    gh = bbox[3] - bbox[1]
    # Horizontally center; vertically align consistently using baseline/ascent
    x = (w - gw) // 2 - bbox[0]
    y = (h - gh) // 2 - bbox[1] if ord(ch) >= 0x80 else (h - FONT_SIZE) // 2 + 1
    draw.text((x, y), ch, fill=255, font=font)
    rows = []
    px = img.load()
    for r in range(h):
        bits = 0
        for c in range(w):
            if px[c, r] >= THRESHOLD:
                bits |= 1 << (w - 1 - c)
        rows.append(bits)
    return rows


def main() -> None:
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    ascii_font = ImageFont.truetype(FONT_PATH, FONT_SIZE - 1)

    zh_chars = sorted({ch for s in UI_STRINGS for ch in s if ord(ch) >= 0x80})

    lines = [
        "// RULES-ACTIVE-GLOBAL",
        "#ifndef CC_FONT_ZH_H",
        "#define CC_FONT_ZH_H",
        "",
        "#include <stdint.h>",
        "",
        f"#define CC_FONT_H       {CELL_H}",
        f"#define CC_FONT_CJK_W   {CJK_W}",
        f"#define CC_FONT_ASCII_W {ASCII_W}",
        f"#define CC_ZH_COUNT     {len(zh_chars)}u",
        "",
        "typedef struct {",
        "    uint16_t cp;",
        "    uint16_t pad;",
        f"    uint32_t rows[{CELL_H}];",
        "} CcZhGlyph;",
        "",
        f"static const uint16_t CC_ASCII_GLYPHS[95][{CELL_H}] = {{",
    ]
    for code in range(0x20, 0x7F):
        ch = chr(code)
        rows = rasterize_glyph(ascii_font, ch, ASCII_W, CELL_H)
        vals = ",".join(f"0x{v:03X}u" for v in rows)
        safe = ch if ch not in ("\\", "'") else ("\\\\" if ch == "\\" else "\\'")
        lines.append(f"    {{{vals}}}, /* '{safe}' */")
    lines.append("};")
    lines.append("")

    lines.append(f"static const CcZhGlyph CC_ZH_GLYPHS[{len(zh_chars)}] = {{")
    for ch in zh_chars:
        rows = rasterize_glyph(font, ch, CJK_W, CELL_H)
        vals = ",".join(f"0x{v:06X}u" for v in rows)
        lines.append(f"    {{ 0x{ord(ch):04X}u, 0u, {{{vals}}} }}, /* {ch} */")
    lines.append("};")
    lines.append("")
    lines.append("#endif /* CC_FONT_ZH_H */")
    lines.append("")

    OUT_H.write_text("\n".join(lines), encoding="utf-8")
    print(f"Generated {OUT_H} ({len(zh_chars)} CJK glyphs + 95 ASCII glyphs, {OUT_H.stat().st_size} bytes header)")


if __name__ == "__main__":
    main()
