"""One-time interactive OAuth helper: open a URL in a browser, capture the
?code= on a localhost redirect. Works over SSH too: paste the redirected URL."""
from __future__ import annotations

import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


def capture_code(authorize_url: str, redirect_uri: str, timeout: int = 300) -> str:
    state = secrets.token_urlsafe(16)
    sep = "&" if "?" in authorize_url else "?"
    url = f"{authorize_url}{sep}state={state}"
    parsed = urllib.parse.urlparse(redirect_uri)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = (qs.get("code") or [None])[0]
            result["state"] = (qs.get("state") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>castsync authorized. You can close this tab.</h2>")

        def log_message(self, *a):
            pass

    server = None
    if parsed.hostname in ("localhost", "127.0.0.1"):
        try:
            server = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
            server.timeout = timeout
            threading.Thread(target=server.handle_request, daemon=True).start()
        except OSError:
            server = None

    print("\nOpen this URL in a browser and approve access:\n\n  " + url + "\n")
    if server:
        print(f"Waiting for the redirect to {redirect_uri} ...")
        print("(If the browser is on another machine, paste the full URL it was redirected to here.)")
    pasted = input("Redirected URL (or just press Enter if the browser was on this machine): ").strip()
    if pasted:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        result["code"] = (qs.get("code") or [None])[0]
        result["state"] = (qs.get("state") or [None])[0]
    if server:
        server.server_close()
    if not result.get("code"):
        raise SystemExit("No authorization code received.")
    if result.get("state") and result["state"] != state:
        raise SystemExit("OAuth state mismatch; aborting.")
    return result["code"]
