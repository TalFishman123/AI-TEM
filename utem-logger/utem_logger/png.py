"""Lossless PNG writer: 8- or 16-bit, grey or RGB, with the state embedded as text so every frame
describes itself.

Pure numpy + zlib (Pillow cannot write 16-bit colour PNGs). Rows use the Sub filter (difference to the
left neighbour), which suits smooth, mostly dark camera frames. An ``sBIT`` chunk records how many
bits are real (10 for the CS165CU: values 0-1023 are the sensor counts as they are). Writes to a
temporary name and renames, so a half-written file is never left behind.
"""
from __future__ import annotations

import os
import struct
import zlib

import numpy as np


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode(img: np.ndarray, text: dict, level=1, significant_bits=None) -> bytes:
    """uint8 or uint16, (H,W) or (H,W,3) -> PNG bytes."""
    img = np.ascontiguousarray(img)
    if img.dtype not in (np.uint8, np.uint16) or img.ndim not in (2, 3) or (img.ndim == 3 and img.shape[2] != 3):
        raise ValueError(f"unsupported image {img.dtype} {img.shape}")
    depth = 8 * img.itemsize
    channels = 3 if img.ndim == 3 else 1
    h, w = img.shape[:2]
    raw = img.astype(img.dtype.newbyteorder(">")).view(np.uint8).reshape(h, -1)   # PNG is big-endian
    bpp = channels * img.itemsize
    sub = raw.copy()
    sub[:, bpp:] = raw[:, bpp:] - raw[:, :-bpp]          # uint8 wraps modulo 256, as PNG wants
    rows = np.hstack([np.ones((h, 1), np.uint8), sub])  # filter type 1 on every row
    out = [b"\x89PNG\r\n\x1a\n",
           _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, 2 if channels == 3 else 0, 0, 0, 0))]
    if significant_bits:
        out.append(_chunk(b"sBIT", bytes([significant_bits] * channels)))
    for k, v in text.items():
        out.append(_chunk(b"tEXt", k.encode("latin-1") + b"\0" + str(v).encode("latin-1", "replace")))
    out += [_chunk(b"IDAT", zlib.compress(rows.tobytes(), level)), _chunk(b"IEND", b"")]
    return b"".join(out)


def save(path, img: np.ndarray, text: dict, level=1, significant_bits=None) -> int:
    """Write `img` to `path` atomically. Returns the file size in bytes."""
    tmp = str(path) + ".part"
    with open(tmp, "wb") as f:
        f.write(encode(img, text, level, significant_bits))
    os.replace(tmp, path)
    return os.path.getsize(path)


def read_text(path) -> dict:
    """The tEXt entries of a PNG written here (state, camera settings), without decoding the image."""
    out = {}
    with open(path, "rb") as f:
        data = f.read()
    i = 8
    while i < len(data):
        n, tag = struct.unpack(">I4s", data[i:i + 8])
        if tag == b"tEXt":
            k, _, v = data[i + 8:i + 8 + n].partition(b"\0")
            out[k.decode("latin-1")] = v.decode("latin-1")
        elif tag == b"IDAT" or tag == b"IEND":
            break
        i += 12 + n
    return out


def load(path):
    """A PNG written here -> numpy array (uint8 or uint16, (H,W) or (H,W,3)), values as saved.
    Any PNG reader works too (imageio, OpenCV with IMREAD_UNCHANGED, Fiji); this one needs only numpy."""
    data = open(path, "rb").read()
    i, idat = 8, []
    while i < len(data):
        n, tag = struct.unpack(">I4s", data[i:i + 8])
        body = data[i + 8:i + 8 + n]
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat.append(body)
        i += 12 + n
    ch = {0: 1, 2: 3}[ctype]
    bpp = ch * depth // 8
    rows = np.frombuffer(zlib.decompress(b"".join(idat)), np.uint8).reshape(h, 1 + w * bpp)
    if not (rows[:, 0] == 1).all():
        raise ValueError("not written by utem_logger (only the Sub filter is decoded); use imageio instead")
    raw = rows[:, 1:].reshape(h, w, bpp)
    raw = np.cumsum(raw, axis=1, dtype=np.uint8)          # undo Sub: running sum modulo 256 per byte
    img = raw.reshape(h, -1).view(">u2" if depth == 16 else np.uint8).astype(np.uint16 if depth == 16 else np.uint8)
    return img.reshape(h, w, 3) if ch == 3 else img.reshape(h, w)
