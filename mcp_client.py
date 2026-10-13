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

PROTOCOL = '2025-06-18'


class MCPError(Exception):
    pass


def command_from_text(s):
    """'python tests\\falesny_mcp.py writer' -> seznam argumentů (Windows cesty, uvozovky kolem mezer)."""
    return [x[1:-1] if len(x) > 1 and x[0] == x[-1] == '"' else x for x in shlex.split(s, posix=False)]


def from_mcp_json(fpath, key):
    """.mcp.json -> (příkaz, env | None) serveru `klic`; None když soubor nebo klíč chybí."""
    try:
        with open(fpath, encoding='utf-8') as f:
            s = json.load(f)['mcpServers'][key]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return [s['command']] + list(s.get('args') or []), s.get('env')


class Client:
    def __init__(self, command, env=None, timeout=300, display_name=None):
        self.command, self.env, self.timeout = list(command), env, timeout
        self.display_name = display_name or os.path.basename(self.command[0])
        self.p, self.q, self.err, self.id = None, queue.Queue(), [], 0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.close()

    def start(self):
        env = dict(os.environ, **(self.env or {}))
        try:
            self.p = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as e:
            raise MCPError('%s fails spustit (%s): %s' % (self.display_name, self.command[0], e))
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        try:
            self._request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                                       'clientInfo': {'name': 'TIAC_Editor', 'version': 'M5'}})
            self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        except MCPError:
            self.close()
            raise

    def _read(self):
        for r in self.p.stdout:
            r = r.strip()
            if r:
                try:
                    self.q.put(json.loads(r.decode('utf-8')))
                except ValueError:
                    self.err.append('nečitelný řádek: ' + r[:200].decode('utf-8', 'replace'))
        self.q.put(None)                                  # konec procesu

    def _read_err(self):
        for r in self.p.stderr:
            self.err.append(r.decode('utf-8', 'replace').rstrip())
            del self.err[:-20]                            # jen posledních 20 řádků pro chybovou zprávu

    def _send(self, message):
        try:
            self.p.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'))
            self.p.stdin.flush()
        except OSError as e:
            raise MCPError('%s neběží (%s)%s' % (self.display_name, e, self._stderr()))

    def _stderr(self):
        return (' — stderr: ' + ' | '.join(self.err[-5:])) if self.err else ''

    def _request(self, method, params, timeout=None):
        self.id += 1
        mine = self.id
        self._send({'jsonrpc': '2.0', 'id': mine, 'method': method, 'params': params})
        while True:
            try:
                z = self.q.get(timeout=timeout or self.timeout)
            except queue.Empty:
                self.close()
                raise MCPError('%s: timeout %d s u %s' % (self.display_name, timeout or self.timeout, method))
            if z is None:
                raise MCPError('%s skončil without odpovědi on %s (kód %s)%s'
                               % (self.display_name, method, self.p.poll(), self._stderr()))
            if 'method' in z and 'id' in z:               # dotaz serveru (ping, roots/list…) -> prázdná odpověď
                self._send({'jsonrpc': '2.0', 'id': z['id'], 'result': {}})
                continue
            if z.get('id') != mine:
                continue                                  # notifikace (progress, log)
            if 'error' in z:
                raise MCPError('%s: %s selhal: %s' % (self.display_name, method, z['error'].get('message', z['error'])))
            return z.get('result') or {}

    def call(self, tool, arguments, timeout=None):
        """tools/call -> text výsledku (content[0].text); isError -> ChybaMCP s textem nástroje."""
        r = self._request('tools/call', {'name': tool, 'arguments': arguments}, timeout)
        text = '\n'.join(c.get('text', '') for c in r.get('content') or [] if c.get('type', 'text') == 'text')
        if r.get('isError'):
            raise MCPError('%s: %s vrátil error: %s' % (self.display_name, tool, text or '(without text)'))
        return text

    def close(self):
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
