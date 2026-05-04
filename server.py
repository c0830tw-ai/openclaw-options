"""
server.py
=========
本地開發 server。功能：
  - 伺服 web/ 目錄（瀏覽 http://localhost:8080）
  - GET /api/refresh  → 同步執行 shioaji_collar.py，完成後回 200
  - GET /api/status   → 回傳目前 script 是否在跑

開啟方式:
    python3 server.py
"""

import os, sys, json, subprocess, threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT    = 8081
ROOT    = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, 'web')
SCRIPT  = os.path.join(ROOT, 'shioaji_collar.py')
DATA    = os.path.join(ROOT, 'latest_collar.json')

_lock    = threading.Lock()
_running = False


def load_env():
    path = os.path.join(ROOT, '.env')
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def do_GET(self):
        if self.path == '/api/refresh':
            self._refresh()
        elif self.path == '/api/data':
            self._data()
        elif self.path == '/api/status':
            self._status()
        else:
            super().do_GET()

    def _refresh(self):
        global _running
        with _lock:
            if _running:
                self._json(202, {'status': 'running'})
                return
            _running = True

        try:
            subprocess.run(
                [sys.executable, SCRIPT],
                env=os.environ.copy(),
                timeout=60,
            )
            self._json(200, {'status': 'ok'})
        except subprocess.TimeoutExpired:
            self._json(504, {'status': 'timeout'})
        except Exception as e:
            self._json(500, {'status': 'error', 'detail': str(e)})
        finally:
            with _lock:
                _running = False

    def _data(self):
        if not os.path.exists(DATA):
            self._json(404, {'error': 'no data yet'})
            return
        with open(DATA, encoding='utf-8') as f:
            raw = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(raw.encode()))
        self.end_headers()
        self.wfile.write(raw.encode())

    def _status(self):
        self._json(200, {'running': _running})

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    def end_headers(self):
        if not getattr(self, 'path', '').startswith('/api'):
            self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def log_message(self, fmt, *args):
        method, path, _ = args[0].split(' ', 2) if ' ' in args[0] else (args[0], '', '')
        if path not in ('/api/status',):
            print(f'  {args[1]}  {path}')


if __name__ == '__main__':
    load_env()
    server = HTTPServer(('', PORT), Handler)
    print(f'OpenClaw Server  →  http://localhost:{PORT}')
    print('開啟瀏覽器後自動抓取最新資料  |  Ctrl+C 停止\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n停止')
