"""tools/gif.py: the LZW stream decodes back to the same pixels (a minimal GIF-style decoder)."""
import random
import unittest

from tests.common import ROOT  # noqa: F401  (puts the repository on sys.path)
from tools.gif import lzw


def decode(data, ms=8):
    clear, end = 1 << ms, (1 << ms) + 1
    acc = bits = i = 0
    size, table, prev, out = ms + 1, None, None, []
    while True:
        while bits < size:
            acc |= data[i] << bits
            bits += 8
            i += 1
        c, acc, bits = acc & ((1 << size) - 1), acc >> size, bits - size
        if c == clear:
            table, size, prev = [bytes([k]) for k in range(256)] + [b'', b''], ms + 1, None
            continue
        if c == end:
            return b''.join(out)
        e = table[c] if c < len(table) else prev + prev[:1]
        out.append(e)
        if prev is not None and len(table) < 4096:
            table.append(prev + e[:1])
            if len(table) == 1 << size and size < 12:
                size += 1
        prev = e


class TestGif(unittest.TestCase):
    def test_lzw_roundtrip(self):
        rnd = random.Random(1)
        for n in (1, 10, 5000, 200000):     # 200000 fills the 4096-entry table several times
            x = bytes(rnd.choice([0, 1, 2, 7, 200]) if rnd.random() < .9 else rnd.randrange(256) for _ in range(n))
            self.assertEqual(decode(lzw(x)), x)


if __name__ == '__main__':
    unittest.main()
