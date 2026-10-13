"""Regenerate tests\\screenshots\\*.png and docs\\demo.gif from the Demo_Line sample with the fake Writer/Reader.

python tools\\screenshots.py [--no-gif]

Everything runs in temp folders (a copy of the sample as the export root, the edit data, the fake "TIA project");
nothing talks to TIA Portal. Needs Chrome or Edge (screenshot.py) and a free port 8765.
"""
import argparse
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import screenshot  # noqa: E402
import server  # noqa: E402
from tests.common import SAMPLES, PROJECT, after_nw2  # noqa: E402
from tools import gif  # noqa: E402

OUT = os.path.join(ROOT, 'tests', 'screenshots')
PORT = 8765                         # the documented port, so the copied MCP config reads http://localhost:8765
FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')


class Demo:
    def __init__(self, root, data, tia=None, slow=None):
        mcp = None
        if tia:
            py = sys.executable
            mcp = {'writer': ([py, FAKE, 'writer', '--state', tia] + (['--slow', str(slow)] if slow else []), None),
                   'reader': ([py, FAKE, 'reader', '--state', tia, '--export-root', root, '--project', PROJECT], None)}
        self.srv = server.create_server(PORT, root, data, mcp=mcp, mcp_timeout=60)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        if r.status >= 400:
            raise RuntimeError('%s %s -> %s %s' % (method, path, r.status, t))
        return t

    def shot(self, page, name, w=1400, h=900, light=False, out=OUT):
        url = 'http://localhost:%d/%s%sscreenshot=1' % (self.port, page, '&' if '?' in page else '?')
        path = screenshot.screenshot(url, os.path.join(out, name), w, h, light)
        print(path)
        return path

    def ai(self):
        self.req('POST', '/api/ai/clients', {'id': 'demo', 'name': 'claude-code', 'version': '2.1'})

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-gif', action='store_true')
    a = ap.parse_args()
    tmp, cwd = tempfile.mkdtemp(), os.getcwd()
    os.chdir(tmp)                     # relative folders: no local paths in the screenshots
    try:
        empty = 'empty'
        os.makedirs(empty)
        d = Demo(empty, os.path.join(tmp, 'data0'))
        d.shot('', 'home_empty.png')
        d.stop()

        root = 'exports'
        shutil.copytree(SAMPLES, root)
        d = Demo(root, os.path.join(tmp, 'data1'))                    # no Writer / Reader configured
        d.req('GET', '/api/tia/status?refresh=1')
        d.shot('', 'home_projects.png')
        d.shot('?project=%s&block=FB_Conveyor&nw=2' % PROJECT, 'lad_viewer.png')
        d.shot('?project=%s&block=FB_Conveyor&nw=4' % PROJECT, 'lad_viewer_light.png', 1400, 700, light=True)
        d.shot('?project=%s&block=FB_Safety&nw=1' % PROJECT, 'fbd_viewer.png', 1400, 700)
        d.stop()

        root2 = 'exports2'                         # the slow snapshot may still write after stop()
        shutil.copytree(SAMPLES, root2)
        d = Demo(root2, os.path.join(tmp, 'data2'), os.path.join(tmp, 'tia2'), slow=2)
        d.req('GET', '/api/tia/status?refresh=1')
        job = d.req('POST', '/api/projects/%s/snapshot' % PROJECT, {})['job']['id']
        d.shot('', 'snapshot_progress.png')
        while d.req('GET', '/api/jobs/' + job)['state'] == 'running':    # the job lock is global: wait before the next server
            time.sleep(0.5)
        d.stop()

        data, tia = 'data', 'tia'
        d = Demo(root, data, tia)
        d.req('GET', '/api/tia/status?refresh=1')
        frames = []
        d.shot('', 'home_tia_attached.png')
        net = d.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=2' % PROJECT)
        u1 = d.req('POST', '/api/edits', {'project': PROJECT, 'round': 'Round 1', 'block': 'FB_Conveyor', 'nw': 2,
                                          'after': after_nw2(net), 'note': 'fuse check before the motor coil'})['id']
        fbd = d.req('GET', '/api/network?project=%s&block=FB_Safety&nw=1' % PROJECT)
        after = d.req('POST', '/api/layout', {'project': PROJECT, 'after': {'language': 'FBD', 'elements': fbd['elements']},
                                              'ops': [{'type': 'add_input', 'id': 'b2', 'operand': '#Door_Closed'}]})['after']
        u2 = d.req('POST', '/api/edits', {'project': PROJECT, 'round': 'Round 1', 'block': 'FB_Safety', 'nw': 1,
                                          'after': after, 'note': 'door closed as a 4th enable condition'})['id']
        d.ai()
        d.shot('', 'home_ai_connected.png')
        frames.append(d.shot('', 'f1.png', 1280, 800, out=tmp))
        frames.append(d.shot('?project=%s&block=FB_Conveyor&nw=2' % PROJECT, 'f2.png', 1280, 800, out=tmp))
        frames.append(d.shot('?edit=' + u1, 'f3.png', 1280, 800, out=tmp))
        d.req('POST', '/api/edits/%s/state' % u1, {'state': 'Approved', 'by': 'user'})
        st = d.req('GET', '/api/state?project=' + PROJECT)
        st['tia'] = {'softwarePath': 'PLC_1', 'plcName': 'PLC_1'}
        d.req('PUT', '/api/state?project=' + PROJECT, st)
        d.shot('?edit=' + u1, 'tia_steps.png', 1400, 1000)
        frames.append(d.shot('?edit=' + u1, 'f4.png', 1280, 800, out=tmp))
        d.req('POST', '/api/edits/%s/upload' % u1, {})
        d.req('POST', '/api/edits/%s/check' % u1, {})
        d.shot('?edit=' + u1, 'edit_checked_upload_check.png', 1400, 1000)
        frames.append(d.shot('?edit=' + u1, 'f5.png', 1280, 800, out=tmp))
        page = '?edit=%s&edit_mode=1&select=b2&input=4' % u2
        d.shot(page, 'fbd_editor.png', 1500, 900)
        frames.append(d.shot(page, 'f6.png', 1280, 800, out=tmp))
        d.stop()
        if not a.no_gif:
            gif.write(os.path.join(ROOT, 'docs', 'demo.gif'), frames, [300, 350, 400, 400, 450, 450])
            print(os.path.join(ROOT, 'docs', 'demo.gif'))
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
