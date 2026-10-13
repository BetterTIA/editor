"""Screenshot of a TIAC_Editor page to PNG through headless Chrome (nothing to install).

python screenshot.py --url "http://localhost:8765/?project=..&block=..&nw=2&screenshot=1" --out nw2.png [--width 1400 --height 900] [--light]
"""
import argparse
import os
import subprocess
import sys
import tempfile

BROWSERS = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',   # fallback, same switches
]


def screenshot(url, out, width=1400, height=900, light=False, wait_ms=4000):
    exe = next((p for p in BROWSERS if os.path.exists(p)), None)
    if not exe:
        raise RuntimeError('neither Chrome nor Edge was found')
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    with tempfile.TemporaryDirectory() as prof:   # own profile: does not clash with a running Chrome
        cmd = [exe, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--user-data-dir=' + prof,
               '--window-size=%d,%d' % (width, height), '--screenshot=' + out,
               # wait for fetch + rendering (the page draws only after the API loads)
               '--virtual-time-budget=%d' % wait_ms]
        if not light:
            cmd.append('--force-dark-mode')
        cmd.append(url)
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError('screenshot was not created: ' + out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--width', type=int, default=1400)
    ap.add_argument('--height', type=int, default=900)
    ap.add_argument('--light', action='store_true', help='light theme (default: dark)')
    a = ap.parse_args()
    print(screenshot(a.url, a.out, a.width, a.height, a.light))


if __name__ == '__main__':
    sys.exit(main())
