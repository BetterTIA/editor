"""Real mouse clicks in the editor (LAD and FBD): headless Chrome via the DevTools protocol, `Input.dispatchMouseEvent`
= trusted sequence mouseMoved → mousePressed → mouseReleased (the browser itself turns it into pointerdown, mousedown,
pointerup, mouseup, click). Nothing to install: a minimal WebSocket client on top of `socket` (stdlib)."""
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

from tests.common import SAMPLES, PROJECT, TS0
import server
import screenshot

CHROME = next((p for p in screenshot.BROWSERS if os.path.exists(p)), None)


class WS:
    """Only what CDP needs: text frames, masked by the client, no extensions."""

    def __init__(self, url):
        host, _, path = url[len('ws://'):].partition('/')
        h, _, p = host.partition(':')
        self.s = socket.create_connection((h, int(p)), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(('GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                        'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n' % (path, host, key)).encode())
        resp = b''
        while b'\r\n\r\n' not in resp:
            resp += self.s.recv(4096)
        if b' 101 ' not in resp.split(b'\r\n')[0]:
            raise RuntimeError(resp[:200])
        self.rest, self.id = resp.split(b'\r\n\r\n', 1)[1], 0

    def _read(self, n):
        while len(self.rest) < n:
            self.rest += self.s.recv(65536)
        x, self.rest = self.rest[:n], self.rest[n:]
        return x

    def receive(self):
        data = b''
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7f
            if n == 126:
                n = struct.unpack('>H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._read(8))[0]
            data += self._read(n)
            if b1 & 0x80:
                return json.loads(data)

    def call(self, method, **params):
        self.id += 1
        d = json.dumps({'id': self.id, 'method': method, 'params': params}).encode()
        mask = os.urandom(4)
        hl = bytes([0x81]) + (bytes([0x80 | len(d)]) if len(d) < 126 else bytes([0x80 | 126]) + struct.pack('>H', len(d))
                              if len(d) < 65536 else bytes([0x80 | 127]) + struct.pack('>Q', len(d)))
        self.s.sendall(hl + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(d)))
        while True:
            m = self.receive()
            if m.get('id') == self.id:
                if 'error' in m:
                    raise RuntimeError(m['error'])
                return m['result']


@unittest.skipUnless(CHROME, 'Chrome/Edge missing')
class TestClicks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp()
        cls.srv = server.create_server(0, SAMPLES, cls.data)
        cls.url = 'http://127.0.0.1:%d' % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.prof = tempfile.mkdtemp()
        cls.chrome = subprocess.Popen([CHROME, '--headless=new', '--disable-gpu', '--remote-debugging-port=0',
                                       '--user-data-dir=' + cls.prof, '--window-size=1500,900', 'about:blank'],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        file = os.path.join(cls.prof, 'DevToolsActivePort')
        content = ''
        for _ in range(100):
            if os.path.exists(file):
                with open(file) as f:
                    content = f.read().strip()
                if content:
                    break
            time.sleep(0.1)
        port = content.split()[0]
        targets = json.loads(urllib.request.urlopen('http://127.0.0.1:%s/json/list' % port, timeout=10).read())
        cls.ws = WS(next(c for c in targets if c['type'] == 'page')['webSocketDebuggerUrl'])

    @classmethod
    def tearDownClass(cls):
        cls.ws.s.close()
        cls.chrome.kill()
        cls.chrome.wait(10)
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.data, ignore_errors=True)
        shutil.rmtree(cls.prof, ignore_errors=True)

    def edit(self, block, nw, fbd):
        net = server.api_network(SAMPLES, PROJECT, block, nw, TS0)
        after = {'language': 'FBD', 'elements': net['elements']} if fbd else {'elements': net['elements'], 'rungs': net['rungs']}
        return server.api_edit_create(SAMPLES, self.data, {'project': PROJECT, 'round': 'test', 'block': block, 'nw': nw,
                                                          'after': after})['id']

    def js(self, expr):
        return self.ws.call('Runtime.evaluate', expression=expr, returnByValue=True, awaitPromise=True)['result'].get('value')

    def wait_for(self, expr, s=10):
        end = time.time() + s
        while time.time() < end:
            if self.js(expr):
                return True
            time.sleep(0.1)
        self.fail('condition not met: ' + expr)

    def open_page(self, fpath):
        self.ws.call('Page.navigate', url=self.url + fpath)
        self.wait_for("document.body && document.body.dataset.done === '1' && !!document.querySelector('#ed-canvas svg')")

    def click(self, selector_js):
        """Real click in the middle of an element (selector_js = JS expression returning the element)."""
        x, y = self.js('(() => { const r = (%s).getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()'
                       % selector_js)
        for etype in ('mouseMoved', 'mousePressed', 'mouseReleased'):
            self.ws.call('Input.dispatchMouseEvent', type=etype, x=x, y=y, button='left', clickCount=1)

    def test_fbd_select_box_and_add_input(self):
        uid = self.edit('FB_Safety', 1, True)
        self.open_page('/?edit=%s&edit_mode=1' % uid)
        self.js("window.__u = []; for (const t of ['pointerdown','mousedown','pointerup','mouseup','click']) "
                "addEventListener(t, e => __u.push(t + (e.isTrusted ? '' : '!')), true); 1")
        box = "[...document.querySelectorAll('#ed-canvas g[data-id]')].find(g => g.querySelector('text.symbol').textContent === '&').querySelector('rect.hit')"
        self.click(box)
        self.wait_for("ed.select === 'b2'")
        self.assertEqual(self.js('__u'), ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'])   # all isTrusted
        self.wait_for("!document.getElementById('ed-plus').disabled")
        self.click("document.getElementById('ed-plus')")
        self.wait_for("ed.after.elements.find(p => p.id === 'b2').inputs.length === 4 && !document.getElementById('ed-undo').disabled")
        # palette -> insert place (new input of &) -> new box n1
        self.click("document.querySelector('#palette .item[data-key=\"OR\"]')")
        self.wait_for("document.querySelectorAll('#ed-canvas .place').length > 0")
        self.click("[...document.querySelectorAll('#ed-canvas .place')].find(c => c.querySelector('title').textContent === 'box b2, input 4')")
        self.wait_for("ed.after.elements.some(p => p.id === 'n1' && p.type === 'or') && ed.select === 'n1'")

    def test_lad_palette_place_and_select(self):
        uid = self.edit('FB_Conveyor', 1, False)
        self.open_page('/?edit=%s&edit_mode=1' % uid)
        self.click("document.querySelector('#ed-canvas g[data-id=\"p1\"] rect.hit')")
        self.wait_for("ed.select === 'p1'")
        self.click("document.querySelector('#palette .item[data-key=\"NO\"]')")
        self.wait_for("document.querySelectorAll('#ed-canvas .place').length > 0")
        self.click("document.querySelector('#ed-canvas .place')")
        self.wait_for("ed.after.elements.some(p => p.id === 'n1')")


    def drag(self, selector_js, dx, dy):
        x, y = self.js('(() => { const r = (%s).getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()'
                       % selector_js)
        self.ws.call('Input.dispatchMouseEvent', type='mousePressed', x=x, y=y, button='left', buttons=1, clickCount=1)
        for i in range(1, 6):
            self.ws.call('Input.dispatchMouseEvent', type='mouseMoved', x=x + dx * i / 5, y=y + dy * i / 5, button='left', buttons=1)
        self.ws.call('Input.dispatchMouseEvent', type='mouseReleased', x=x + dx, y=y + dy, button='left', buttons=0, clickCount=1)

    def test_hmi_designer_insert_move_resize_delete(self):
        uid = server.api_edit_create(SAMPLES, self.data, {'project': PROJECT, 'round': 'test', 'kind': 'hmi_screen',
                                                          'screen': 'Manual'})['id']
        self.ws.call('Page.navigate', url=self.url + '/?edit=%s&edit_mode=1' % uid)
        self.wait_for("document.body && document.body.dataset.done === '1' && !!document.querySelector('#ed-canvas .hmi-screen')")
        self.click("document.querySelector('#palette .item[data-type=\"Button\"]')")
        self.wait_for("he.insert === 'Button'")
        self.click("document.querySelector('#ed-canvas .hmi-screen')")                       # the middle of the screen
        self.wait_for("he.sel === 'Button_1' && he.after.items.some(x => x.name === 'Button_1' && x.left % 8 === 0)")
        io = "document.querySelector('#ed-canvas .hi[data-name=\"IO_Setpoint\"]')"
        self.drag(io, 60, 0)                                           # snapped to the 8 px grid (top 220 -> 224)
        self.wait_for("(x => x.left > 260 && x.left % 8 === 0 && x.top === 224)(he.after.items.find(x => x.name === 'IO_Setpoint'))")
        self.drag("document.querySelector('#ed-canvas .hh.se')", 40, 20)
        self.wait_for("(x => x.width > 160 && x.height > 40 && x.width % 8 === 0)(he.after.items.find(x => x.name === 'IO_Setpoint'))")
        self.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Delete'})); 1")
        self.wait_for("!he.after.items.some(x => x.name === 'IO_Setpoint')")
        self.click("document.getElementById('ed-undo')")
        self.wait_for("he.after.items.some(x => x.name === 'IO_Setpoint')")


if __name__ == '__main__':
    unittest.main()
