#!/usr/bin/env python3
"""veya Production Server — lightweight deployment for veya.aiinote.com

Serves the SPA dashboard (veya/web/) and proxies API calls to the
FastAPI backend. Starts instantly with zero heavy imports.

Usage:  python3 veya/server/prod_server.py [--port PORT]
"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

PORT = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == '--port' else 8080
BACKEND_PORT = 8765
WEB_DIR = Path(__file__).parent.parent / "web"
BACKEND_PROC = None

# ── MIME types ─────────────────────────────────
MIME = {
    '.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript',
    '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon', '.woff2': 'font/woff2',
}

class VeyaHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_POST(self):
        self._proxy_to_backend('POST')

    def do_GET(self):
        if self.path.startswith('/api/'):
            self._proxy_to_backend('GET')
        elif self.path == '/' or '.' not in os.path.basename(self.path):
            self.path = '/index.html'
            super().do_GET()
        else:
            super().do_GET()

    def _proxy_to_backend(self, method):
        try:
            url = f'http://127.0.0.1:{BACKEND_PORT}{self.path}'
            body = None
            content_len = int(self.headers.get('Content-Length', 0))
            if content_len > 0:
                body = self.rfile.read(content_len)
            req = urllib.request.Request(url, data=body, method=method)
            for key, val in self.headers.items():
                if key.lower() not in ('host', 'content-length'):
                    req.add_header(key, val)
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.send_response(resp.status)
                for key, val in resp.headers.items():
                    if key.lower() not in ('transfer-encoding', 'content-encoding'):
                        self.send_header(key, val)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(e.read())
        except Exception:
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'backend_starting', 'message': 'Backend is initializing...'}).encode())

def start_backend_async():
    """Start FastAPI backend in background (non-blocking)."""
    global BACKEND_PROC
    try:
        BACKEND_PROC = subprocess.Popen(
            [sys.executable, '-m', 'veya.server.app', '--host', '127.0.0.1', '--port', str(BACKEND_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f'  Backend starting on :{BACKEND_PORT} (may take ~30s)...')
    except Exception as e:
        print(f'  ⚠ Backend start failed: {e}')

if __name__ == '__main__':
    print(f'''
╔══════════════════════════════════════════╗
║         ⚡ veya Production Server        ║
║       https://veya.aiinote.com          ║
╠══════════════════════════════════════════╣
║  Frontend => http://0.0.0.0:{PORT}       ║
║  Backend  => http://127.0.0.1:{BACKEND_PORT}     ║
║  SPA      => {WEB_DIR} ║
╚══════════════════════════════════════════╝
''')

    start_backend_async()

    with socketserver.ThreadingTCPServer(('0.0.0.0', PORT), VeyaHandler) as httpd:
        print(f'  ✅ Ready at http://0.0.0.0:{PORT}')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\n  Shutting down...')
        finally:
            if BACKEND_PROC:
                BACKEND_PROC.terminate()
