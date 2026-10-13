"""Minimal MCP client over stdio (JSON-RPC 2.0, one message = one line of JSON) — stdlib only.

Usage:
    with Client(['TiaMcpServer.exe', '--allow-write'], env={...}, timeout=300) as k:
        text = k.call('Connect', {})
Handshake `initialize` -> notification `notifications/initialized`; `tools/call` returns `result.content[0].text`,
`result.isError` -> MCPError. All errors (command cannot start, timeout, process exit, tool error)
are MCPError with a readable message.
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
    """'python tests\\fake_mcp.py writer' -> argument list (Windows paths, quotes around spaces)."""
    return [x[1:-1] if len(x) > 1 and x[0] == x[-1] == '"' else x for x in shlex.split(s, posix=False)]


def from_mcp_json(fpath, key):
    """.mcp.json -> (command, env | None) of server `key`; None when the file or the key is missing."""
    try:
        with open(fpath, encoding='utf-8') as f:
            s = json.load(f)['mcpServers'][key]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return [s['command']] + list(s.get('args') or []), s.get('env')


class Client:
    def __init__(self, command, env=None, timeout=300, display_name=None, client_info=None):
        self.command, self.env, self.timeout = list(command), env, timeout
        self.client_info = client_info or {'name': 'TIAC_Editor', 'version': 'M5'}
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
            raise MCPError('%s cannot be started (%s): %s' % (self.display_name, self.command[0], e))
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        try:
            self._request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                                       'clientInfo': self.client_info})
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
                    self.err.append('unreadable line: ' + r[:200].decode('utf-8', 'replace'))
        self.q.put(None)                                  # process ended

    def _read_err(self):
        for r in self.p.stderr:
            self.err.append(r.decode('utf-8', 'replace').rstrip())
            del self.err[:-20]                            # keep only the last 20 lines for the error message

    def _send(self, message):
        try:
            self.p.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'))
            self.p.stdin.flush()
        except OSError as e:
            raise MCPError('%s is not running (%s)%s' % (self.display_name, e, self._stderr()))

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
                raise MCPError('%s: timeout %d s on %s' % (self.display_name, timeout or self.timeout, method))
            if z is None:
                raise MCPError('%s exited without answering %s (exit code %s)%s'
                               % (self.display_name, method, self.p.poll(), self._stderr()))
            if 'method' in z and 'id' in z:               # server request (ping, roots/list…) -> empty answer
                self._send({'jsonrpc': '2.0', 'id': z['id'], 'result': {}})
                continue
            if z.get('id') != mine:
                continue                                  # notification (progress, log)
            if 'error' in z:
                raise MCPError('%s: %s failed: %s' % (self.display_name, method, z['error'].get('message', z['error'])))
            return z.get('result') or {}

    def list_tools(self, timeout=None):
        return self._request('tools/list', {}, timeout).get('tools') or []

    def call_raw(self, tool, arguments, timeout=None):
        """tools/call -> the whole result {content: [...], isError?} (images, errors as data)."""
        return self._request('tools/call', {'name': tool, 'arguments': arguments}, timeout)

    def call(self, tool, arguments, timeout=None):
        """tools/call -> result text (content[0].text); isError -> MCPError with the tool's text."""
        r = self._request('tools/call', {'name': tool, 'arguments': arguments}, timeout)
        text = '\n'.join(c.get('text', '') for c in r.get('content') or [] if c.get('type', 'text') == 'text')
        if r.get('isError'):
            raise MCPError('%s: %s returned an error: %s' % (self.display_name, tool, text or '(no text)'))
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
