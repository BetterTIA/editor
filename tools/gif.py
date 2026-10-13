"""Animated GIF from PNG screenshots, standard library only: PNG decode (zlib + unfilter), one shared
256-colour palette (most frequent colours, others mapped to the nearest), LZW, GIF89a with a loop.

write(out, [png, ...], [delay in 1/100 s, ...])
"""
import struct
import zlib


def read_png(path):
    """-> (width, height, bytes RGB). 8-bit RGB/RGBA, not interlaced (what headless Chrome writes)."""
    with open(path, 'rb') as f:
        data = f.read()
    assert data[:8] == b'\x89PNG\r\n\x1a\n', path
    pos, idat = 8, b''
    while pos < len(data):
        n, kind = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b'IHDR':
            w, h, depth, ctype, _, _, lace = struct.unpack('>IIBBBBB', body)
            assert depth == 8 and ctype in (2, 6) and not lace, 'unsupported PNG: ' + path
        elif kind == b'IDAT':
            idat += body
        pos += 12 + n
    bpp = 4 if ctype == 6 else 3
    raw, stride = zlib.decompress(idat), w * bpp
    out, prev = bytearray(), bytearray(stride)
    for y in range(h):
        ft, row = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if ft == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 255
        elif ft == 2:
            row = bytearray((a + b) & 255 for a, b in zip(row, prev))
        elif ft == 3:
            for i in range(stride):
                row[i] = (row[i] + ((row[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
        elif ft == 4:
            for i in range(stride):
                a, b, c = (row[i - bpp] if i >= bpp else 0), prev[i], (prev[i - bpp] if i >= bpp else 0)
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[i] = (row[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += row
        prev = row
    if bpp == 4:
        del out[3::4]
    return w, h, bytes(out)


def lzw(indices, min_size=8):
    clear, end = 1 << min_size, (1 << min_size) + 1
    out, acc, nbits = bytearray(), 0, 0
    size, table, nxt = min_size + 1, {}, end + 1

    def emit(code):
        nonlocal acc, nbits
        acc |= code << nbits
        nbits += size
        while nbits >= 8:
            out.append(acc & 255)
            acc >>= 8
            nbits -= 8
    emit(clear)
    cur = indices[0]
    for k in indices[1:]:
        key = cur << 8 | k
        if key in table:
            cur = table[key]
            continue
        emit(cur)
        if nxt == 4096:                       # table full: start over
            emit(clear)
            table, nxt, size = {}, end + 1, min_size + 1
        else:
            table[key] = nxt
            nxt += 1
            if nxt > (1 << size) and size < 12:
                size += 1
        cur = k
    emit(cur)
    emit(end)
    if nbits:
        out.append(acc & 255)
    return bytes(out)


def write(out, pngs, delays):
    frames = [read_png(p) for p in pngs]
    w, h = frames[0][0], frames[0][1]
    assert all(f[:2] == (w, h) for f in frames), 'frames must have the same size'
    count = {}
    for _, _, px in frames:
        for i in range(0, len(px), 3):
            c = px[i:i + 3]
            count[c] = count.get(c, 0) + 1
    palette = sorted(count, key=count.get, reverse=True)[:256]
    index = {c: i for i, c in enumerate(palette)}
    pal_rgb = [tuple(c) for c in palette]
    gif = bytearray(b'GIF89a' + struct.pack('<HHBBB', w, h, 0xF7, 0, 0))
    gif += b''.join(palette) + b'\0\0\0' * (256 - len(palette))
    gif += b'\x21\xff\x0bNETSCAPE2.0\x03\x01\0\0\0'          # loop forever
    for (_, _, px), delay in zip(frames, delays):
        idx = bytearray(len(px) // 3)
        for j in range(len(idx)):
            c = px[3 * j:3 * j + 3]
            i = index.get(c)
            if i is None:                                    # nearest palette colour, cached
                r, g, b = c
                i = index[c] = min(range(len(pal_rgb)), key=lambda n: (pal_rgb[n][0] - r) ** 2 + (pal_rgb[n][1] - g) ** 2
                                   + (pal_rgb[n][2] - b) ** 2)
            idx[j] = i
        gif += b'\x21\xf9\x04\x04' + struct.pack('<H', delay) + b'\0\0'
        gif += b'\x2c' + struct.pack('<HHHHB', 0, 0, w, h, 0) + b'\x08'
        data = lzw(idx)
        for k in range(0, len(data), 255):
            gif += bytes([len(data[k:k + 255])]) + data[k:k + 255]
        gif += b'\0'
    gif += b'\x3b'
    with open(out, 'wb') as f:
        f.write(gif)
