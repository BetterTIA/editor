"""M5: Upload (TIAC_Writer) a Check (TIAC_Reader) přes API s falešnými MCP servery — bez TIA.

Export root i „projekt TIA" falešných serverů jsou v temp: Writer exportuje blok z vzorku (nebo z --stav po importu),
import zapíše do --stav; Reader exportuje z --stav do nové složky export rootu serveru."""
import contextlib
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from tests.common import KOREN, VZORKY, PROJEKT, TS0
import check
import s7dcl
import server

FALESNY = os.path.join(KOREN, 'tests', 'fake_mcp.py')
TIA = {'softwarePath': 'PLC_1', 'plcName': 'PLC_1'}


class TestUploadCheck(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.tia = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        shutil.copytree(os.path.join(VZORKY, PROJEKT, TS0), os.path.join(self.root, PROJEKT, TS0))
        self.spust()

    def spust(self, *writer_navic):
        py = sys.executable
        mcp = {'writer': ([py, FALESNY, 'writer', '--state', self.tia] + list(writer_navic), None),
               'reader': ([py, FALESNY, 'reader', '--state', self.tia, '--export-root', self.root, '--projekt', PROJEKT], None)}
        self.srv = server.vytvor_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def zastav(self):
        self.srv.shutdown()
        self.srv.server_close()

    def tearDown(self):
        self.zastav()
        for d in (self.root, self.data, self.tia):
            shutil.rmtree(d, ignore_errors=True)

    def req(self, metoda, cesta, telo=None):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=60)
        c.request(metoda, cesta, body=json.dumps(telo).encode() if telo is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def nastav_tia(self, tia=TIA):
        _, st = self.req('GET', '/api/stav?projekt=%s' % PROJEKT)
        st['tia'] = tia
        self.assertEqual(self.req('PUT', '/api/stav?projekt=%s' % PROJEKT, st)[0], 200)

    def navrh(self, blok='FB_Dopravnik', nw=1, ops=None, state='Approved'):
        _, net = self.req('GET', '/api/network?projekt=%s&blok=%s&nw=%d' % (PROJEKT, blok, nw))
        po = {'jazyk': 'FBD', 'prvky': net['prvky']} if net['jazyk'] == 'FBD' else {'prvky': net['prvky'], 'rungy': net['rungy']}
        if ops:
            s, r = self.req('POST', '/api/rozlozeni', {'projekt': PROJEKT, 'po': po, 'ops': ops})
            self.assertEqual(s, 200, r)
            po = r['po']
        s, u = self.req('POST', '/api/uprava', {'projekt': PROJEKT, 'kolo': 'M5', 'blok': blok, 'nw': nw, 'po': po})
        self.assertEqual(s, 201, u)
        if state != 'Proposal':
            self.req('POST', '/api/uprava/%s/stav' % u['id'], {'stav': 'Approved'})
        return u['id']

    def importy(self):
        with open(os.path.join(self.tia, 'importy.jsonl'), encoding='utf-8') as f:
            return [json.loads(r) for r in f]

    OPS = [{'typ': 'vloz', 'misto': {'r': 0, 'k': 3}, 'prvek': 'NO'},
           {'typ': 'zmen', 'id': 'n1', 'hodnoty': {'operand': '"Pojistka_OK"'}}]

    def test_upload_f_bloku_a_bez_tia_409(self):
        uid_f = self.navrh('FB_Bezpecnost', 1)
        uid = self.navrh(ops=self.OPS)
        s, e = self.req('POST', '/api/uprava/%s/upload' % uid)
        self.assertEqual(s, 409)
        self.assertIn('nastavení TIA', e['chyba'])
        self.nastav_tia()
        s, e = self.req('POST', '/api/uprava/%s/upload' % uid_f)
        self.assertEqual(s, 409)
        self.assertIn('F-blok', e['chyba'])
        s, e = self.req('POST', '/api/uprava/%s/check' % uid)                 # Check jen u Signed
        self.assertEqual(s, 409)
        uid_p = self.navrh(state='Proposal')
        self.assertEqual(self.req('POST', '/api/uprava/%s/upload' % uid_p)[0], 409)
        self.assertFalse(os.path.exists(os.path.join(self.tia, 'importy.jsonl')))       # nic se nezapsalo
        _, st = self.req('GET', '/api/stav?projekt=%s' % PROJEKT)
        st['tia'] = {'softwarePath': 1}
        self.assertEqual(self.req('PUT', '/api/stav?projekt=%s' % PROJEKT, st)[0], 400)

    def test_upload_check_checked(self):
        self.nastav_tia()
        uid = self.navrh(ops=self.OPS)
        s, r = self.req('POST', '/api/uprava/%s/upload' % uid)
        self.assertEqual(s, 200, r)
        self.assertTrue(r['ok'], r)
        u = r['uprava']
        self.assertEqual((u['stav'], u['historie'][-1]['kdo']), ('Signed', 'TIAC_Writer'))
        self.assertEqual([k['krok'] for k in u['zapisy'][0]['kroky']],
                         ['Connect', 'ResolveObjectPath', 'ExportBlocksAsDocuments', 'Náhrada networku v .s7dcl',
                          'PreviewImport', 'ImportFromDocuments'])
        imp = self.importy()
        self.assertEqual((len(imp), imp[0]['groupPath'], imp[0]['importOption'], imp[0]['fileNameWithoutExtension']),
                         (1, 'Dopravnik', 'Override', 'FB_Dopravnik'))
        b = s7dcl.nacti_blok(imp[0]['soubor'])                                  # importovaný .s7dcl má network „po"
        self.assertEqual([p.get('operand') for p in b['networky'][0]['prvky']],
                         ['"Start_Povoleno"', '"DB_Stroj".Bezpecnost_OK', '"DB_Stroj".Rezim_Auto', '"Pojistka_OK"', '#Pripraven'])
        self.assertEqual(len(b['networky']), 8)
        # Check: Reader exportuje to, co Writer importoval -> shoda -> Checked
        s, c = self.req('POST', '/api/uprava/%s/check' % uid)
        self.assertEqual(s, 200, c)
        self.assertTrue(c['shoda'], c)
        self.assertEqual((c['uprava']['stav'], c['uprava']['historie'][-1]['kdo']), ('Checked', 'TIAC_Reader'))
        self.assertEqual(c['uprava']['kontroly'][-1]['export_ts'], c['export_ts'])
        self.assertTrue(os.path.isdir(os.path.join(self.root, PROJEKT, c['export_ts'], 'PLC_1', 'Blocks', 'Dopravnik')))
        self.assertEqual([z['nastroj'] for z in c['uprava']['zapisy']], ['TIAC_Writer', 'TIAC_Reader'])

    def test_check_neshoda_zustane_signed_a_checked_409(self):
        self.nastav_tia()
        uid = self.navrh(ops=self.OPS)
        self.assertTrue(self.req('POST', '/api/uprava/%s/upload' % uid)[1]['ok'])
        soubor = self.importy()[0]['soubor']                                    # „někdo" v TIA změnil operand
        with open(soubor, encoding='utf-8') as f:
            t = f.read()
        with open(soubor, 'w', encoding='utf-8') as f:
            f.write(t.replace('"Pojistka_OK"', '"Pojistka_XX"'))
        s, c = self.req('POST', '/api/uprava/%s/check' % uid)
        self.assertEqual((s, c['ok'], c['shoda'], c['uprava']['stav']), (200, False, False, 'Signed'))
        self.assertIn('Pojistka_XX', c['zprava'])
        self.assertIs(c['uprava']['kontroly'][-1]['shoda'], False)
        s, e = self.req('POST', '/api/uprava/%s/stav' % uid, {'stav': 'Checked', 'kdo': 'Honza'})
        self.assertEqual(s, 409)                                                 # Checked bez poslední shody
        out = io.StringIO()                                                      # CLI --check: neshoda = exit 1
        with contextlib.redirect_stdout(out):
            self.assertEqual(check.main(['--server', self.url, '--check', uid]), 1)
        self.assertTrue(out.getvalue().startswith('NESHODA %s · stav Signed' % uid))
        with open(soubor, 'w', encoding='utf-8') as f:                           # oprava v TIA -> Check projde
            f.write(t)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(check.main(['--server', self.url, '--check', uid]), 0)
        self.assertTrue(out.getvalue().startswith('CHECKED %s · stav Checked' % uid))

    def test_tia_zaneprazdnene(self):
        self.zastav()
        self.spust('--vice-instanci')
        self.nastav_tia()
        uid = self.navrh(ops=self.OPS)
        s, r = self.req('POST', '/api/uprava/%s/upload' % uid)
        self.assertEqual((s, r['ok'], r['uprava']['stav']), (200, False, 'Approved'))
        self.assertTrue(r['zprava'].startswith('TIA zaneprázdněné nebo Writer/Reader nejde spustit: '))
        self.assertIn('More than one', r['zprava'])
        self.assertEqual(r['uprava']['zapisy'][0]['kroky'][0]['ok'], False)
        self.zastav()                                                            # nespustitelný Writer
        self.srv = server.vytvor_server(0, self.root, self.data, mcp={'writer': ([os.path.join(self.tia, 'neni.exe')], None)})
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        s, r = self.req('POST', '/api/uprava/%s/upload' % uid)
        self.assertEqual((s, r['ok']), (200, False))
        self.assertIn('nejde spustit', r['zprava'])
        s, r = self.req('GET', '/api/uprava/%s' % uid)
        self.assertEqual((r['tia'], len(r['uprava']['zapisy'])), (TIA, 2))

    def test_zamek(self):
        self.nastav_tia()
        uid = self.navrh(ops=self.OPS)
        self.assertTrue(server.ZAMEK_TIA.acquire(blocking=False))
        try:
            s, e = self.req('POST', '/api/uprava/%s/upload' % uid)
        finally:
            server.ZAMEK_TIA.release()
        self.assertEqual(s, 409)
        self.assertIn('jiný Upload', e['chyba'])

    def test_upload_fbd_ne_safety(self):
        """FBD network v ne-F bloku (FC_Vetve NW3: O + Coil) — upload, import obsahuje nový vstup, Check shoda."""
        self.nastav_tia()
        uid = self.navrh('FC_Vetve', 3, ops=[{'typ': 'pridej_vstup', 'id': 'b1', 'operand': '"Senzor_C"'}])
        s, r = self.req('POST', '/api/uprava/%s/upload' % uid)
        self.assertTrue(r['ok'], r)
        with open(self.importy()[0]['soubor'], encoding='utf-8') as f:
            self.assertIn('in3 := "Senzor_C"', f.read())
        s, c = self.req('POST', '/api/uprava/%s/check' % uid)
        self.assertEqual((c['shoda'], c['uprava']['stav']), (True, 'Checked'))


if __name__ == '__main__':
    unittest.main()
