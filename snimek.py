"""Snímek stránky LADEditoru do PNG přes headless Chrome (bez instalací).

python snimek.py --url "http://localhost:8765/?projekt=..&blok=..&nw=2&snimek=1" --out nw2.png [--width 1400 --height 900] [--svetly]
"""
import argparse
import os
import subprocess
import sys
import tempfile

PROHLIZECE = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',   # záloha, stejné přepínače
]


def snimek(url, out, width=1400, height=900, svetly=False, cekani_ms=4000):
    exe = next((p for p in PROHLIZECE if os.path.exists(p)), None)
    if not exe:
        raise RuntimeError('Chrome ani Edge nenalezen')
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    with tempfile.TemporaryDirectory() as prof:   # vlastní profil: nekoliduje s běžícím Chromem
        cmd = [exe, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--user-data-dir=' + prof,
               '--window-size=%d,%d' % (width, height), '--screenshot=' + out,
               # počká na fetch + vykreslení (stránka kreslí až po načtení API)
               '--virtual-time-budget=%d' % cekani_ms]
        if not svetly:
            cmd.append('--force-dark-mode')
        cmd.append(url)
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError('snímek nevznikl: ' + out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--width', type=int, default=1400)
    ap.add_argument('--height', type=int, default=900)
    ap.add_argument('--svetly', action='store_true', help='světlý motiv (výchozí tmavý)')
    a = ap.parse_args()
    print(snimek(a.url, a.out, a.width, a.height, a.svetly))


if __name__ == '__main__':
    sys.exit(main())
