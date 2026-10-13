"""Skutečné kliky myší v editoru (LAD i FBD): headless Chrome přes DevTools protokol, `Input.dispatchMouseEvent`
= důvěryhodná sekvence mouseMoved → mousePressed → mouseReleased (prohlížeč z ní sám udělá pointerdown, mousedown,
pointerup, mouseup, click). Žádná instalace: minimální WebSocket klient nad `socket` (stdlib)."""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

from tests.common import VZORKY, PROJEKT, TS0
import server
import screenshot

CHROME = next((p for p in screenshot.PROHLIZECE if os.path.exists(p)), None)


class WS:
    """Jen to, co potřebuje CDP: textové rámce, maskované od klienta, bez rozšíření."""

    def __init__(self, url):
        host, _, path = url[len('ws://'):].partition('/')
        h, _, p = host.partition(':')
        self.s = socket.create_connection((h, int(p)), timeout=20)
        klic = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(('GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                        'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n' % (path, host, klic)).encode())
        odp = b''
        while b'\r\n\r\n' not in odp:
            odp += self.s.recv(4096)
        if b' 101 ' not in odp.split(b'\r\n')[0]:
            raise RuntimeError(odp[:200])
        self.zbytek, self.id = odp.split(b'\r\n\r\n', 1)[1], 0

    def _cti(self, n):
        while len(self.zbytek) < n:
            self.zbytek += self.s.recv(65536)
        x, self.zbytek = self.zbytek[:n], self.zbytek[n:]
        return x

    def prijmi(self):
        data = b''
        while True:
            b1, b2 = self._cti(2)
            n = b2 & 0x7f
            if n == 126:
                n = struct.unpack('>H', self._cti(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._cti(8))[0]
            data += self._cti(n)
            if b1 & 0x80:
                return json.loads(data)

    def volej(self, metoda, **params):
        self.id += 1
        d = json.dumps({'id': self.id, 'method': metoda, 'params': params}).encode()
        maska = os.urandom(4)
        hl = bytes([0x81]) + (bytes([0x80 | len(d)]) if len(d) < 126 else bytes([0x80 | 126]) + struct.pack('>H', len(d))
                              if len(d) < 65536 else bytes([0x80 | 127]) + struct.pack('>Q', len(d)))
        self.s.sendall(hl + maska + bytes(c ^ maska[i % 4] for i, c in enumerate(d)))
        while True:
            m = self.prijmi()
            if m.get('id') == self.id:
                if 'error' in m:
                    raise RuntimeError(m['error'])
                return m['result']


@unittest.skipUnless(CHROME, 'chybí Chrome/Edge')
class TestKlikani(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp()
        cls.srv = server.vytvor_server(0, VZORKY, cls.data)
        cls.url = 'http://127.0.0.1:%d' % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.prof = tempfile.mkdtemp()
        cls.chrome = subprocess.Popen([CHROME, '--headless=new', '--disable-gpu', '--remote-debugging-port=0',
                                       '--user-data-dir=' + cls.prof, '--window-size=1500,900', 'about:blank'],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        soubor = os.path.join(cls.prof, 'DevToolsActivePort')
        obsah = ''
        for _ in range(100):
            if os.path.exists(soubor):
                with open(soubor) as f:
                    obsah = f.read().strip()
                if obsah:
                    break
            time.sleep(0.1)
        port = obsah.split()[0]
        cile = json.loads(urllib.request.urlopen('http://127.0.0.1:%s/json/list' % port, timeout=10).read())
        cls.ws = WS(next(c for c in cile if c['type'] == 'page')['webSocketDebuggerUrl'])

    @classmethod
    def tearDownClass(cls):
        cls.ws.s.close()
        cls.chrome.kill()
        cls.chrome.wait(10)
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.data, ignore_errors=True)
        shutil.rmtree(cls.prof, ignore_errors=True)

    def uprava(self, blok, nw, fbd):
        net = server.api_network(VZORKY, PROJEKT, blok, nw, TS0)
        po = {'jazyk': 'FBD', 'prvky': net['prvky']} if fbd else {'prvky': net['prvky'], 'rungy': net['rungy']}
        return server.api_uprava_nova(VZORKY, self.data, {'projekt': PROJEKT, 'kolo': 'test', 'blok': blok, 'nw': nw,
                                                          'po': po})['id']

    def js(self, vyraz):
        return self.ws.volej('Runtime.evaluate', expression=vyraz, returnByValue=True, awaitPromise=True)['result'].get('value')

    def cekej(self, vyraz, s=10):
        konec = time.time() + s
        while time.time() < konec:
            if self.js(vyraz):
                return True
            time.sleep(0.1)
        self.fail('nesplněno: ' + vyraz)

    def otevri(self, cesta):
        self.ws.volej('Page.navigate', url=self.url + cesta)
        self.cekej("document.body && document.body.dataset.hotovo === '1' && !!document.querySelector('#ed-platno svg')")

    def klik(self, selektor_js):
        """Skutečný klik doprostřed prvku (selektor_js = JS výraz vracející element)."""
        x, y = self.js('(() => { const r = (%s).getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()'
                       % selektor_js)
        for typ in ('mouseMoved', 'mousePressed', 'mouseReleased'):
            self.ws.volej('Input.dispatchMouseEvent', type=typ, x=x, y=y, button='left', clickCount=1)

    def test_fbd_vyber_boxu_a_plus_vstup(self):
        uid = self.uprava('FB_Bezpecnost', 1, True)
        self.otevri('/?uprava=%s&upravit=1' % uid)
        self.js("window.__u = []; for (const t of ['pointerdown','mousedown','pointerup','mouseup','click']) "
                "addEventListener(t, e => __u.push(t + (e.isTrusted ? '' : '!')), true); 1")
        box = "[...document.querySelectorAll('#ed-platno g[data-id]')].find(g => g.querySelector('text.titul').textContent === '&').querySelector('rect.hit')"
        self.klik(box)
        self.cekej("ed.vyber === 'b2'")
        self.assertEqual(self.js('__u'), ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'])   # vše isTrusted
        self.cekej("!document.getElementById('ed-plus').disabled")
        self.klik("document.getElementById('ed-plus')")
        self.cekej("ed.po.prvky.find(p => p.id === 'b2').vstupy.length === 4 && !document.getElementById('ed-zpet').disabled")
        # paleta -> místo vložení (nový vstup &) -> nový box n1
        self.klik("document.querySelector('#paleta .pol[data-klic=\"OR\"]')")
        self.cekej("document.querySelectorAll('#ed-platno .misto').length > 0")
        self.klik("[...document.querySelectorAll('#ed-platno .misto')].find(c => c.querySelector('title').textContent === 'box b2, vstup 4')")
        self.cekej("ed.po.prvky.some(p => p.id === 'n1' && p.typ === 'or') && ed.vyber === 'n1'")

    def test_lad_paleta_misto_a_vyber(self):
        uid = self.uprava('FB_Dopravnik', 1, False)
        self.otevri('/?uprava=%s&upravit=1' % uid)
        self.klik("document.querySelector('#ed-platno g[data-id=\"p1\"] rect.hit')")
        self.cekej("ed.vyber === 'p1'")
        self.klik("document.querySelector('#paleta .pol[data-klic=\"NO\"]')")
        self.cekej("document.querySelectorAll('#ed-platno .misto').length > 0")
        self.klik("document.querySelector('#ed-platno .misto')")
        self.cekej("ed.po.prvky.some(p => p.id === 'n1')")


if __name__ == '__main__':
    unittest.main()
