"""Minimální MCP klient přes stdio (JSON-RPC 2.0, zpráva = jeden řádek JSON) — jen stdlib (M5).

Použití:
    with Klient(['TiaMcpServer.exe', '--allow-write'], env={...}, timeout=300) as k:
        text = k.zavolej('Connect', {})
Handshake `initialize` -> notifikace `notifications/initialized`; `tools/call` vrací `result.content[0].text`,
`result.isError` -> ChybaMCP. Všechny chyby (nespustitelný příkaz, timeout, konec procesu, chyba nástroje)
jsou ChybaMCP s čitelnou zprávou.
"""
import json
import os
import queue
import shlex
import subprocess
import threading

PROTOKOL = '2025-06-18'


class ChybaMCP(Exception):
    pass


def prikaz_z_textu(s):
    """'python tests\\falesny_mcp.py writer' -> seznam argumentů (Windows cesty, uvozovky kolem mezer)."""
    return [x[1:-1] if len(x) > 1 and x[0] == x[-1] == '"' else x for x in shlex.split(s, posix=False)]


def z_mcp_json(cesta, klic):
    """.mcp.json -> (příkaz, env | None) serveru `klic`; None když soubor nebo klíč chybí."""
    try:
        with open(cesta, encoding='utf-8') as f:
            s = json.load(f)['mcpServers'][klic]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return [s['command']] + list(s.get('args') or []), s.get('env')


class Klient:
    def __init__(self, prikaz, env=None, timeout=300, jmeno=None):
        self.prikaz, self.env, self.timeout = list(prikaz), env, timeout
        self.jmeno = jmeno or os.path.basename(self.prikaz[0])
        self.p, self.q, self.err, self.id = None, queue.Queue(), [], 0

    def __enter__(self):
        self.spust()
        return self

    def __exit__(self, *a):
        self.zavri()

    def spust(self):
        env = dict(os.environ, **(self.env or {}))
        try:
            self.p = subprocess.Popen(self.prikaz, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as e:
            raise ChybaMCP('%s nejde spustit (%s): %s' % (self.jmeno, self.prikaz[0], e))
        threading.Thread(target=self._cti, daemon=True).start()
        threading.Thread(target=self._cti_err, daemon=True).start()
        try:
            self._dotaz('initialize', {'protocolVersion': PROTOKOL, 'capabilities': {},
                                       'clientInfo': {'name': 'TIAC_Editor', 'version': 'M5'}})
            self._posli({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        except ChybaMCP:
            self.zavri()
            raise

    def _cti(self):
        for r in self.p.stdout:
            r = r.strip()
            if r:
                try:
                    self.q.put(json.loads(r.decode('utf-8')))
                except ValueError:
                    self.err.append('nečitelný řádek: ' + r[:200].decode('utf-8', 'replace'))
        self.q.put(None)                                  # konec procesu

    def _cti_err(self):
        for r in self.p.stderr:
            self.err.append(r.decode('utf-8', 'replace').rstrip())
            del self.err[:-20]                            # jen posledních 20 řádků pro chybovou zprávu

    def _posli(self, zprava):
        try:
            self.p.stdin.write((json.dumps(zprava, ensure_ascii=False) + '\n').encode('utf-8'))
            self.p.stdin.flush()
        except OSError as e:
            raise ChybaMCP('%s neběží (%s)%s' % (self.jmeno, e, self._stderr()))

    def _stderr(self):
        return (' — stderr: ' + ' | '.join(self.err[-5:])) if self.err else ''

    def _dotaz(self, metoda, params, timeout=None):
        self.id += 1
        moje = self.id
        self._posli({'jsonrpc': '2.0', 'id': moje, 'method': metoda, 'params': params})
        while True:
            try:
                z = self.q.get(timeout=timeout or self.timeout)
            except queue.Empty:
                self.zavri()
                raise ChybaMCP('%s: timeout %d s u %s' % (self.jmeno, timeout or self.timeout, metoda))
            if z is None:
                raise ChybaMCP('%s skončil bez odpovědi na %s (kód %s)%s'
                               % (self.jmeno, metoda, self.p.poll(), self._stderr()))
            if 'method' in z and 'id' in z:               # dotaz serveru (ping, roots/list…) -> prázdná odpověď
                self._posli({'jsonrpc': '2.0', 'id': z['id'], 'result': {}})
                continue
            if z.get('id') != moje:
                continue                                  # notifikace (progress, log)
            if 'error' in z:
                raise ChybaMCP('%s: %s selhal: %s' % (self.jmeno, metoda, z['error'].get('message', z['error'])))
            return z.get('result') or {}

    def zavolej(self, nastroj, argumenty, timeout=None):
        """tools/call -> text výsledku (content[0].text); isError -> ChybaMCP s textem nástroje."""
        r = self._dotaz('tools/call', {'name': nastroj, 'arguments': argumenty}, timeout)
        text = '\n'.join(c.get('text', '') for c in r.get('content') or [] if c.get('type', 'text') == 'text')
        if r.get('isError'):
            raise ChybaMCP('%s: %s vrátil chybu: %s' % (self.jmeno, nastroj, text or '(bez textu)'))
        return text

    def zavri(self):
        if not self.p:
            return
        if self.p.poll() is None:
            try:
                self.p.stdin.close()
                self.p.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.p.kill()
                self.p.wait()
        for r in (self.p.stdin, self.p.stdout, self.p.stderr):
            try:
                r.close()
            except OSError:
                pass
