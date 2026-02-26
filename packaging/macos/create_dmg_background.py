#!/usr/bin/env python3
"""Generate DMG background image for Video Masa (no external dependencies).

Creates a 600x400 dark-themed PNG with:
- A right-pointing arrow between the app icon and Applications positions
- "Drag Video Masa into Applications" text centered below

Usage: python3 create_dmg_background.py [output_path]
"""
import struct
import zlib
import sys

WIDTH, HEIGHT = 600, 400
BG = (18, 18, 28)            # Dark background matching app theme
ARROW = (200, 255, 0)        # Lime green (brand color #c8ff00)
TEXT = (140, 140, 160)        # Subtle gray text

# Minimal 5x7 bitmap font for A-Z, a-z, 0-9, and basic punctuation
# Each character is 5 columns wide, 7 rows tall, stored as 7 ints (bit masks)
FONT = {
    'A': [0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    'B': [0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110],
    'C': [0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110],
    'D': [0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110],
    'E': [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111],
    'F': [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000],
    'G': [0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01110],
    'H': [0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    'I': [0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    'J': [0b00111, 0b00010, 0b00010, 0b00010, 0b00010, 0b10010, 0b01100],
    'K': [0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001],
    'L': [0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111],
    'M': [0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001],
    'N': [0b10001, 0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001],
    'O': [0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    'P': [0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000],
    'Q': [0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101],
    'R': [0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001],
    'S': [0b01110, 0b10001, 0b10000, 0b01110, 0b00001, 0b10001, 0b01110],
    'T': [0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100],
    'U': [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    'V': [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100],
    'W': [0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b10101, 0b01010],
    'X': [0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001],
    'Y': [0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100],
    'Z': [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111],
    'a': [0b00000, 0b00000, 0b01110, 0b00001, 0b01111, 0b10001, 0b01111],
    'b': [0b10000, 0b10000, 0b10110, 0b11001, 0b10001, 0b10001, 0b11110],
    'c': [0b00000, 0b00000, 0b01110, 0b10000, 0b10000, 0b10001, 0b01110],
    'd': [0b00001, 0b00001, 0b01101, 0b10011, 0b10001, 0b10001, 0b01111],
    'e': [0b00000, 0b00000, 0b01110, 0b10001, 0b11111, 0b10000, 0b01110],
    'f': [0b00110, 0b01001, 0b01000, 0b11100, 0b01000, 0b01000, 0b01000],
    'g': [0b00000, 0b01111, 0b10001, 0b10001, 0b01111, 0b00001, 0b01110],
    'h': [0b10000, 0b10000, 0b10110, 0b11001, 0b10001, 0b10001, 0b10001],
    'i': [0b00100, 0b00000, 0b01100, 0b00100, 0b00100, 0b00100, 0b01110],
    'j': [0b00010, 0b00000, 0b00110, 0b00010, 0b00010, 0b10010, 0b01100],
    'k': [0b10000, 0b10000, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010],
    'l': [0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    'm': [0b00000, 0b00000, 0b11010, 0b10101, 0b10101, 0b10001, 0b10001],
    'n': [0b00000, 0b00000, 0b10110, 0b11001, 0b10001, 0b10001, 0b10001],
    'o': [0b00000, 0b00000, 0b01110, 0b10001, 0b10001, 0b10001, 0b01110],
    'p': [0b00000, 0b00000, 0b11110, 0b10001, 0b11110, 0b10000, 0b10000],
    'q': [0b00000, 0b00000, 0b01101, 0b10011, 0b01111, 0b00001, 0b00001],
    'r': [0b00000, 0b00000, 0b10110, 0b11001, 0b10000, 0b10000, 0b10000],
    's': [0b00000, 0b00000, 0b01110, 0b10000, 0b01110, 0b00001, 0b11110],
    't': [0b01000, 0b01000, 0b11100, 0b01000, 0b01000, 0b01001, 0b00110],
    'u': [0b00000, 0b00000, 0b10001, 0b10001, 0b10001, 0b10011, 0b01101],
    'v': [0b00000, 0b00000, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100],
    'w': [0b00000, 0b00000, 0b10001, 0b10001, 0b10101, 0b10101, 0b01010],
    'x': [0b00000, 0b00000, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001],
    'y': [0b00000, 0b00000, 0b10001, 0b10001, 0b01111, 0b00001, 0b01110],
    'z': [0b00000, 0b00000, 0b11111, 0b00010, 0b00100, 0b01000, 0b11111],
    ' ': [0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b00000],
}


def put_pixel(buf, x, y, r, g, b):
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        off = (y * WIDTH + x) * 3
        buf[off] = r
        buf[off + 1] = g
        buf[off + 2] = b


def draw_rect(buf, x1, y1, x2, y2, color):
    for y in range(max(0, y1), min(HEIGHT, y2 + 1)):
        for x in range(max(0, x1), min(WIDTH, x2 + 1)):
            put_pixel(buf, x, y, *color)


def draw_arrow(buf, x1, y, x2, color, thickness=3, head_w=14, head_h=20):
    """Draw a horizontal arrow from x1 to x2 at vertical center y."""
    # Shaft
    half = thickness // 2
    draw_rect(buf, x1, y - half, x2 - head_w, y + half, color)
    # Arrowhead (triangle pointing right)
    for dy in range(-head_h // 2, head_h // 2 + 1):
        # Width of this row of the triangle
        progress = 1.0 - abs(dy) / (head_h / 2)
        tip_x = x2
        base_x = x2 - head_w
        row_end = int(base_x + (tip_x - base_x) * progress)
        for x in range(base_x, row_end + 1):
            put_pixel(buf, x, y + dy, *color)


def draw_text(buf, text, cx, cy, color, scale=2):
    """Render text centered at (cx, cy) using bitmap font."""
    char_w = 5 * scale + scale  # character width + spacing
    total_w = len(text) * char_w - scale
    start_x = cx - total_w // 2
    start_y = cy - (7 * scale) // 2

    for ci, ch in enumerate(text):
        glyph = FONT.get(ch, FONT.get(' '))
        if glyph is None:
            continue
        bx = start_x + ci * char_w
        for row in range(7):
            bits = glyph[row]
            for col in range(5):
                if bits & (1 << (4 - col)):
                    # Draw scaled pixel block
                    px = bx + col * scale
                    py = start_y + row * scale
                    for sy in range(scale):
                        for sx in range(scale):
                            put_pixel(buf, px + sx, py + sy, *color)


def write_png(buf, path):
    """Write raw RGB buffer as PNG (no dependencies)."""

    def chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)

    # Build raw image data with filter byte (0 = None) per row
    raw = bytearray()
    for y in range(HEIGHT):
        raw.append(0)  # filter: None
        off = y * WIDTH * 3
        raw.extend(buf[off:off + WIDTH * 3])

    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')  # PNG signature
        f.write(chunk(b'IHDR', struct.pack('>IIBBBBB', WIDTH, HEIGHT, 8, 2, 0, 0, 0)))
        f.write(chunk(b'IDAT', zlib.compress(bytes(raw), 9)))
        f.write(chunk(b'IEND', b''))


def main():
    buf = bytearray(BG * (WIDTH * HEIGHT))  # Fill background

    # Arrow between icon positions (150, 180) and (450, 180)
    draw_arrow(buf, 215, 180, 395, ARROW)

    # Text below the arrow
    draw_text(buf, "Drag Video Masa into Applications", WIDTH // 2, 280, TEXT, scale=2)

    output = sys.argv[1] if len(sys.argv) > 1 else "dmg_background.png"
    write_png(buf, output)
    print(f"  [+] Background image: {output}")


if __name__ == "__main__":
    main()
