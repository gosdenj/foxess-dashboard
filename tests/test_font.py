"""Render the embedded font as ASCII art so mistakes are visible.

    python3 tests/test_font.py

Prints every printable character and a sample sentence, so the glyph table can
be eyeballed without flashing a device.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "display"))

from font5x7 import FONT_HEIGHT, GLYPHS, glyph, text_height, text_width  # noqa: E402


def render(text: str) -> str:
    lines = [""] * FONT_HEIGHT
    for ch in text:
        cols = glyph(ch)
        for x in range(5):
            bits = cols[x]
            for y in range(FONT_HEIGHT):
                if bits & (1 << y):
                    lines[y] += "#"
                else:
                    lines[y] += "."
        lines = [line + "." for line in lines]   # 1px gap between glyphs
    return "\n".join(lines)


def main() -> int:
    print("Sample: PV 3.42 kW   LOAD 1.56 kW   SOC 88%\n")
    print(render("PV 3.42 kW"))
    print()
    print(render("LOAD 1.56 kW"))
    print()
    print(render("SOC 88%"))
    print()
    print(render("GRID -0.61 kW"))
    print()
    print("Every printable character:\n")
    printable = [c for c in GLYPHS if c != " "]
    for start in range(0, len(printable), 16):
        chunk = printable[start:start + 16]
        block = [row.split(".") for row in render("".join(chunk)).split("\n")]
        for row in block:
            print("  " + "".join(row))
        print()

    # sanity checks on the table itself
    problems = []
    for ch, cols in GLYPHS.items():
        if len(cols) != 5:
            problems.append(f"{ch!r} has {len(cols)} columns")
        for b in cols:
            if not 0 <= b <= 0xFF:
                problems.append(f"{ch!r} column out of range")
    if len(GLYPHS) < 90:
        problems.append(f"only {len(GLYPHS)} glyphs")

    print(f"glyphs: {len(GLYPHS)}   "
          f"'PV 3.42' width @scale1: {text_width('PV 3.42')}px, "
          f"@scale2: {text_width('PV 3.42', 2)}px, "
          f"height: {text_height()}px")

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print("  " + p)
        return 1
    print("\nfont table OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
