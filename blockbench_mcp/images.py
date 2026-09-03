"""纹理位图工具：data URL 编解码、最小 PNG 解码、平均色采样。"""

from __future__ import annotations

import base64
import struct
import zlib
from typing import Any

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def bytes_to_data_url(png: bytes) -> str:
    """把 PNG 字节转成 Blockbench 纹理使用的 data URL。"""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def data_url_to_bytes(data_url: str | None) -> bytes | None:
    """从 data URL 还原字节；非 data URL 返回 None。"""
    if not data_url:
        return None
    if not data_url.startswith("data:"):
        return None
    head, _, payload = data_url.partition(",")
    if "base64" not in head:
        raise ValueError("仅支持 base64 编码的 data URL")
    return base64.b64decode(payload)


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """解析 PNG 顶层块：返回 [(tag, payload)]。"""
    if not data.startswith(PNG_HEADER):
        raise ValueError("不是合法的 PNG 文件头")
    out: list[tuple[bytes, bytes]] = []
    pos = len(PNG_HEADER)
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        out.append((tag, payload))
        pos += 12 + length
        if tag == b"IEND":
            break
    return out


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png_rgba(data: bytes) -> tuple[int, int, list[list[tuple[int, int, int, int]]]]:
    """解码 PNG 为 RGBA 像素（仅支持非交织、8 位色深、调色板 <=8 位）。

    返回 (width, height, rows)；rows[y][x] = (r, g, b, a)。这是给纹理预览
    采样用的最小实现，正式解码请依赖 Pillow 等库（本仓库保持零依赖）。
    """
    chunks = _chunks(data)
    width = height = bit_depth = color_type = 0
    idat = bytearray()
    palette: list[tuple[int, int, int]] = []
    for tag, payload in chunks:
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", payload[:10])
            interlace = payload[12]
            if interlace != 0:
                raise ValueError("暂不支持 Adam7 交织 PNG")
        elif tag == b"PLTE":
            palette = [
                (payload[i], payload[i + 1], payload[i + 2])
                for i in range(0, len(payload), 3)
            ]
        elif tag == b"IDAT":
            idat.extend(payload)
    if not width or not height:
        raise ValueError("PNG 缺少 IHDR/尺寸")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    if color_type != 3 and bit_depth != 8:
        raise ValueError(f"不支持的位深 {bit_depth}（仅 8 位真彩色/灰度）")
    if color_type == 3 and bit_depth not in (1, 2, 4, 8):
        raise ValueError(f"不支持的调色板位深 {bit_depth}")
    bits_per_pixel = channels * bit_depth
    stride = (width * bits_per_pixel + 7) // 8
    bpp = max(1, bits_per_pixel // 8)
    raw = zlib.decompress(bytes(idat))

    def unfilter() -> list[bytearray]:
        rows: list[bytearray] = []
        prev = bytearray(stride)
        pos = 0
        for _ in range(height):
            if pos >= len(raw):
                raise ValueError("PNG 数据不足")
            ftype = raw[pos]
            line = bytearray(raw[pos + 1 : pos + 1 + stride])
            pos += 1 + stride
            if ftype == 1:  # Sub
                for i in range(bpp, stride):
                    line[i] = (line[i] + line[i - bpp]) & 0xFF
            elif ftype == 2:  # Up
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif ftype == 3:  # Average
                for i in range(stride):
                    left = line[i - bpp] if i >= bpp else 0
                    line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
            elif ftype == 4:  # Paeth
                for i in range(stride):
                    left = line[i - bpp] if i >= bpp else 0
                    up = prev[i]
                    up_left = prev[i - bpp] if i >= bpp else 0
                    line[i] = (line[i] + _paeth(left, up, up_left)) & 0xFF
            elif ftype != 0:
                raise ValueError(f"未知 PNG 滤波类型 {ftype}")
            rows.append(line)
            prev = line
        return rows

    rows8 = unfilter()

    def sample_bit(pos: int, row: int) -> int:
        byte = rows8[row][pos // 8]
        shift = 8 - bit_depth - (pos % 8)
        return (byte >> shift) & ((1 << bit_depth) - 1)

    out: list[list[tuple[int, int, int, int]]] = []
    max_idx = (1 << bit_depth) - 1
    for y in range(height):
        row = rows8[y]
        line: list[tuple[int, int, int, int]] = []
        if color_type == 6:
            for x in range(width):
                i = x * 4
                line.append((row[i], row[i + 1], row[i + 2], row[i + 3]))
        elif color_type == 2:
            for x in range(width):
                i = x * 3
                line.append((row[i], row[i + 1], row[i + 2], 255))
        elif color_type == 4:
            for x in range(width):
                i = x * 2
                line.append((row[i], row[i], row[i], row[i + 1]))
        elif color_type == 0:
            for x in range(width):
                v = row[x]
                line.append((v, v, v, 255))
        elif color_type == 3:
            for x in range(width):
                idx = sample_bit(x * bit_depth, y) if bit_depth < 8 else row[x]
                if idx >= len(palette):
                    raise ValueError(f"调色板索引越界 {idx}")
                r, g, b = palette[idx]
                line.append((r, g, b, 255))
        else:
            raise ValueError(f"不支持的 PNG 颜色类型 {color_type}")
        out.append(line)
    return width, height, out


def sample_rect_average(
    pixels: list[list[tuple[int, int, int, int]]],
    width: int,
    height: int,
    uv: list[float],
) -> tuple[int, int, int]:
    """取 UV 矩形内像素平均色（alpha 与白色混合），返回 (r, g, b)。

    uv 语义与 Blockbench 一致：像素坐标 [x1, y1, x2, y2]。
    空矩形（或全透明）退化为整张画布平均，避免预览全黑。
    """

    def sample_all() -> tuple[int, int, int]:
        acc = [0, 0, 0]
        n = 0
        for y in range(height):
            row = pixels[y]
            for x in range(width):
                r, g, b, a = row[x]
                k = a / 255.0
                acc[0] += r * k
                acc[1] += g * k
                acc[2] += b * k
                n += k
        if n <= 0:
            return (180, 180, 180)
        return tuple(int(round(c / n)) for c in acc)  # type: ignore[return-value]

    u0 = int(round(min(uv[0], uv[2])))
    u1 = int(round(max(uv[0], uv[2])))
    v0 = int(round(min(uv[1], uv[3])))
    v1 = int(round(max(uv[1], uv[3])))
    if u1 - u0 < 1 or v1 - v0 < 1:
        return sample_all()
    u0 = max(0, min(width - 1, u0))
    u1 = max(u0 + 1, min(width, u1))
    v0 = max(0, min(height - 1, v0))
    v1 = max(v0 + 1, min(height, v1))
    acc = [0.0, 0.0, 0.0]
    alpha_sum = 0.0
    for y in range(v0, v1):
        row = pixels[y]
        for x in range(u0, u1):
            r, g, b, a = row[x]
            k = a / 255.0
            acc[0] += r * k
            acc[1] += g * k
            acc[2] += b * k
            alpha_sum += k
    if alpha_sum <= 1e-9:
        return sample_all()
    return tuple(int(round(c / alpha_sum)) for c in acc)  # type: ignore[return-value]
