#!/usr/bin/env python3
"""
ClusterPulse Bootstrapper.
Compiled to a single exe with PyInstaller. Never changes after first release.

Steps:
  1. Load cached credentials (encrypted) or show browser popup to collect them
  2. SSH to version-source bastion, read version
  3. If version differs from local cache → SFTP pull entire program directory
  4. Launch main.py as subprocess
  5. Exit (bootstrapper is done)

This file has no health-check logic whatsoever.
Only imports: paramiko, cryptography, subprocess, pathlib, http.server, webbrowser
"""
from __future__ import annotations
import http.server
import json
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

# ── Local cache dir ───────────────────────────────────────────────────────────
CACHE_DIR   = Path.home() / ".clusterpulse"
CACHE_FILE  = CACHE_DIR / "credentials.cache"
VERSION_FILE= CACHE_DIR / "version.txt"
PROGRAM_DIR = CACHE_DIR / "program"

BOOTSTRAP_PORT = 19099
REMOTE_ROOT    = "/opt/clusterpulse"
REMOTE_VERSION = f"{REMOTE_ROOT}/version.txt"


# ── Credential encryption (Fernet from cryptography — already in paramiko deps) ──

def _derive_key(password: str, salt: bytes) -> bytes:
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def save_creds(host: str, port: int, username: str, password: str) -> None:
    import base64, os
    from cryptography.fernet import Fernet
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    salt   = os.urandom(16)
    fernet = Fernet(_derive_key(password, salt))
    ciph   = fernet.encrypt(json.dumps(
        {"host": host, "port": port, "username": username, "password": password}
    ).encode())
    CACHE_FILE.write_bytes(base64.b64encode(salt) + b"\n" + ciph)
    try: os.chmod(CACHE_FILE, 0o600)
    except Exception: pass


def load_creds(password: str) -> Optional[dict]:
    import base64
    from cryptography.fernet import Fernet, InvalidToken
    if not CACHE_FILE.exists(): return None
    try:
        raw  = CACHE_FILE.read_bytes().split(b"\n", 1)
        salt = base64.b64decode(raw[0])
        fernet = Fernet(_derive_key(password, salt))
        return json.loads(fernet.decrypt(raw[1]).decode())
    except Exception:
        return None


# ── Browser credential popup ──────────────────────────────────────────────────

_creds_received: Optional[dict] = None
_server_done    = threading.Event()


class _CredHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass   # suppress server logs

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_CRED_HTML.encode())

    def do_POST(self):
        global _creds_received
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length).decode()
        params = {k: v[0] for k, v in parse_qs(body).items()}
        _creds_received = {
            "host":     params.get("host", "").strip(),
            "port":     int(params.get("port", "22") or "22"),
            "username": params.get("username", "").strip(),
            "password": params.get("password", "").strip(),
            "remember": params.get("remember", "") == "on",
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_DONE_HTML.encode())
        threading.Thread(target=_server_done.set, daemon=True).start()


_CRED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClusterPulse — Connect</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'IBM Plex Sans',system-ui,sans-serif;background:#0f1117;
     color:#e8edf8;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#171923;border:1px solid #2a3350;border-radius:10px;
      padding:36px 40px;width:420px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:28px}
.logo-icon{width:36px;height:36px;background:linear-gradient(135deg,#f59e0b,#ef4444);
           border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px}
.logo-name{font-family:'IBM Plex Mono',monospace;font-size:16px;font-weight:600}
.logo-sub{font-size:11px;color:#7a8aaa}
h2{font-size:15px;font-weight:600;margin-bottom:6px}
p{font-size:12px;color:#7a8aaa;margin-bottom:24px;line-height:1.5}
label{display:block;font-size:11px;color:#7a8aaa;text-transform:uppercase;
      letter-spacing:.5px;margin-bottom:5px}
input[type=text],input[type=number],input[type=password]{
  width:100%;background:#1e2333;border:1px solid #2a3350;border-radius:5px;
  padding:9px 13px;color:#e8edf8;font-family:'IBM Plex Mono',monospace;font-size:13px;
  outline:none;margin-bottom:14px;transition:border-color .15s}
input:focus{border-color:#60a5fa}
.row2{display:grid;grid-template-columns:3fr 1fr;gap:10px}
.chk-row{display:flex;align-items:center;gap:8px;margin-bottom:20px;font-size:12px;color:#7a8aaa}
input[type=checkbox]{accent-color:#f59e0b;width:14px;height:14px}
button{width:100%;padding:11px;background:linear-gradient(135deg,#f59e0b,#ef4444);
       color:#0f1117;border:none;border-radius:5px;font-size:13px;font-weight:700;
       font-family:'IBM Plex Mono',monospace;cursor:pointer;letter-spacing:.5px;
       transition:opacity .15s}
button:hover{opacity:.88}
.err{color:#ef4444;font-size:12px;margin-bottom:12px;display:none}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">⚡</div>
    <div><div class="logo-name">ClusterPulse</div>
         <div class="logo-sub">Version Source Connection</div></div>
  </div>
  <h2>Connect to Version Source</h2>
  <p>Enter the credentials for the version-source bastion server.<br>
     These are only used to check for updates and download the program.</p>
  <div class="err" id="err">Please fill in all required fields.</div>
  <form method="POST" onsubmit="return validate()">
    <label>Bastion Host / IP</label>
    <input type="text" name="host" placeholder="192.168.1.10" required>
    <div class="row2">
      <div>
        <label>Username</label>
        <input type="text" name="username" placeholder="root" required>
      </div>
      <div>
        <label>Port</label>
        <input type="number" name="port" value="22" min="1" max="65535">
      </div>
    </div>
    <label>Password</label>
    <input type="password" name="password" placeholder="••••••••" required>
    <div class="chk-row">
      <input type="checkbox" name="remember" id="remember" checked>
      <label for="remember" style="margin-bottom:0;text-transform:none;letter-spacing:0">
        Remember credentials (encrypted locally)</label>
    </div>
    <button type="submit">Connect →</button>
  </form>
</div>
<script>
function validate(){
  const host=document.querySelector('[name=host]').value.trim();
  const user=document.querySelector('[name=username]').value.trim();
  const pass=document.querySelector('[name=password]').value;
  if(!host||!user||!pass){document.getElementById('err').style.display='block';return false}
  return true;
}
</script>
</body>
</html>"""

_DONE_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>body{font-family:system-ui;background:#0f1117;color:#22c55e;
display:flex;align-items:center;justify-content:center;height:100vh;font-size:18px}</style>
</head><body>✓ Connected — you can close this tab.</body></html>"""


def _get_credentials() -> dict:
    """Load cached credentials or show browser popup."""
    # Try to load without asking first (if cache exists, try empty password prompt later)
    # For now: if cache file exists but we can't decrypt without password, still popup
    # Simple approach: always popup but pre-fill from a separate unencrypted host/user hint file
    hint_file = CACHE_DIR / "hint.json"
    hint = {}
    if hint_file.exists():
        try: hint = json.loads(hint_file.read_text())
        except Exception: pass

    # Start mini HTTP server
    server = http.server.HTTPServer(("127.0.0.1", BOOTSTRAP_PORT), _CredHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    webbrowser.open(f"http://127.0.0.1:{BOOTSTRAP_PORT}")
    _server_done.wait()           # block until form submitted
    server.shutdown()

    creds = _creds_received
    if creds.get("remember"):
        save_creds(creds["host"], creds["port"], creds["username"], creds["password"])
        # Save unencrypted hint (host + username only, no password)
        hint_file.write_text(json.dumps({"host": creds["host"], "username": creds["username"]}))
    return creds


# ── SFTP sync ─────────────────────────────────────────────────────────────────

def _sftp_pull(sftp, remote_dir: str, local_dir: Path):
    import stat
    local_dir.mkdir(parents=True, exist_ok=True)
    for entry in sftp.listdir_attr(remote_dir):
        rpath = f"{remote_dir}/{entry.filename}"
        lpath = local_dir / entry.filename
        if stat.S_ISDIR(entry.st_mode):
            _sftp_pull(sftp, rpath, lpath)
        else:
            sftp.get(rpath, str(lpath))


def _sync(host: str, port: int, username: str, password: str) -> str:
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, port=port, username=username, password=password,
                   timeout=30, banner_timeout=30, auth_timeout=30)
    sftp = client.open_sftp()
    try:
        with sftp.open(REMOTE_VERSION) as fh:
            remote_ver = fh.read().decode().strip()

        local_ver = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else ""

        if local_ver != remote_ver:
            print(f"[bootstrap] Syncing {local_ver} → {remote_ver}")
            _sftp_pull(sftp, REMOTE_ROOT, PROGRAM_DIR)
            VERSION_FILE.write_text(remote_ver)
            print("[bootstrap] Sync complete")
        else:
            print(f"[bootstrap] Version {remote_ver} up to date")

        return remote_ver
    finally:
        sftp.close()
        client.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    creds = _get_credentials()

    print(f"[bootstrap] Connecting to {creds['host']}:{creds['port']} …")
    try:
        _sync(creds["host"], creds["port"], creds["username"], creds["password"])
    except Exception as e:
        # Show error in browser and exit
        print(f"[bootstrap] ERROR: {e}")
        _show_error(str(e))
        sys.exit(1)

    # Launch main program
    main_py = PROGRAM_DIR / "main.py"
    if not main_py.exists():
        _show_error(f"main.py not found at {main_py}. Check version-source deployment.")
        sys.exit(1)

    print("[bootstrap] Launching ClusterPulse…")
    subprocess.Popen([sys.executable, str(main_py)],
                     cwd=str(PROGRAM_DIR))
    # Bootstrapper exits — main.py owns everything from here


def _show_error(message: str):
    """Show a quick browser error page."""
    class _EH(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.end_headers()
            html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{font-family:system-ui;background:#0f1117;color:#ef4444;
display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:16px}}
p{{color:#7a8aaa;font-size:13px}}</style></head>
<body><h2>⚡ ClusterPulse — Connection Error</h2>
<p>{message}</p><p>Please check credentials and network, then try again.</p></body></html>"""
            self.wfile.write(html.encode())
    srv = http.server.HTTPServer(("127.0.0.1", BOOTSTRAP_PORT+1), _EH)
    t   = threading.Thread(target=lambda: srv.serve_forever(), daemon=True)
    t.start()
    webbrowser.open(f"http://127.0.0.1:{BOOTSTRAP_PORT+1}")
    import time; time.sleep(8)


if __name__ == "__main__":
    main()
