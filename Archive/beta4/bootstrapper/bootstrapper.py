#!/usr/bin/env python3
"""
CloudHealth Beta4 Bootstrapper.
Compiled to a single exe with PyInstaller — never changes after first release.
Steps: credential popup → SSH version check → SFTP sync if needed → launch main.py
"""
from __future__ import annotations
import http.server, json, os, subprocess, sys, threading, webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

CACHE_DIR      = Path.home() / ".cloud_health"
CACHE_FILE     = CACHE_DIR / ".creds_cache"
SALT_FILE      = CACHE_DIR / ".salt"
VERSION_FILE   = CACHE_DIR / "version.txt"
PROGRAM_DIR    = CACHE_DIR / "program"
BOOTSTRAP_PORT = 9000
REMOTE_ROOT    = "/opt/cloud_health"
REMOTE_VERSION = f"{REMOTE_ROOT}/version.txt"


def _derive_key(password: str, salt: bytes) -> bytes:
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def _get_salt() -> bytes:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if SALT_FILE.exists():
        return SALT_FILE.read_bytes()
    salt = os.urandom(16)
    SALT_FILE.write_bytes(salt)
    try: os.chmod(SALT_FILE, 0o600)
    except Exception: pass
    return salt


def save_creds(host: str, port: int, username: str, password: str):
    import base64
    from cryptography.fernet import Fernet
    fernet = Fernet(_derive_key(password, _get_salt()))
    ciph   = fernet.encrypt(json.dumps(
        {"host": host, "port": port, "username": username, "password": password}
    ).encode())
    CACHE_FILE.write_bytes(ciph)
    try: os.chmod(CACHE_FILE, 0o600)
    except Exception: pass


def load_creds(password: str) -> Optional[dict]:
    from cryptography.fernet import Fernet
    if not CACHE_FILE.exists(): return None
    try:
        fernet = Fernet(_derive_key(password, _get_salt()))
        return json.loads(fernet.decrypt(CACHE_FILE.read_bytes()).decode())
    except Exception: return None


_creds: Optional[dict] = None
_done  = threading.Event()


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        meta = {}
        meta_file = CACHE_DIR / ".meta"
        if meta_file.exists():
            try: meta = json.loads(meta_file.read_text())
            except: pass
        self.send_response(200)
        self.send_header("Content-Type","text/html;charset=utf-8")
        self.end_headers()
        self.wfile.write(_html(meta).encode())

    def do_POST(self):
        global _creds
        length = int(self.headers.get("Content-Length",0))
        body   = self.rfile.read(length).decode()
        params = {k:v[0] for k,v in parse_qs(body).items()}
        _creds = {"host": params.get("host","").strip(),
                  "port": int(params.get("port","22") or "22"),
                  "username": params.get("user","").strip(),
                  "password": params.get("pass","").strip(),
                  "remember": params.get("remember","")=="on"}
        self.send_response(200)
        self.send_header("Content-Type","text/html;charset=utf-8")
        self.end_headers()
        self.wfile.write(_done_html().encode())
        threading.Thread(target=_done.set, daemon=True).start()


def _html(meta: dict) -> str:
    ip   = meta.get("ip","")
    user = meta.get("user","")
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>CloudHealth Setup</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui;background:#0f172a;color:#e8edf8;
     display:flex;align-items:center;justify-content:center;height:100vh}}
.card{{background:#171923;border:1px solid #2a3350;border-radius:10px;
       padding:2.25rem 2.5rem;width:420px}}
h1{{font-family:monospace;font-size:1.3rem;font-weight:700;color:#818cf8;margin-bottom:1.5rem}}
label{{display:block;font-size:.75rem;color:#7a8aaa;text-transform:uppercase;
       letter-spacing:.04em;margin-bottom:.3rem}}
input{{width:100%;background:#1e2333;border:1px solid #2a3350;border-radius:5px;
       padding:.65rem .85rem;color:#e8edf8;font-size:.88rem;margin-bottom:1rem}}
.row2{{display:grid;grid-template-columns:3fr 1fr;gap:.75rem}}
.chk{{display:flex;align-items:center;gap:.5rem;font-size:.82rem;color:#7a8aaa;margin-bottom:1.25rem}}
button{{width:100%;padding:.85rem;background:linear-gradient(135deg,#6366f1,#8b5cf6);
        color:#fff;border:none;border-radius:5px;font-size:.9rem;font-weight:700;cursor:pointer}}
</style></head><body><div class="card">
<h1>⚡ CloudHealth Setup</h1>
<form method="POST">
  <label>Version-Source Bastion IP</label>
  <input name="ip" value="{ip}" placeholder="10.x.x.x" required>
  <div class="row2">
    <div><label>Username</label><input name="user" value="{user}" required></div>
    <div><label>Port</label><input name="port" value="22" type="number"></div>
  </div>
  <label>Password</label><input name="pass" type="password" required>
  <div class="chk"><input type="checkbox" name="remember" checked id="r">
    <label for="r" style="margin-bottom:0;text-transform:none">Remember credentials</label></div>
  <button type="submit">Connect &amp; Launch →</button>
</form></div></body></html>"""


def _done_html() -> str:
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
body{font-family:system-ui;background:#0f172a;color:#22c55e;
display:flex;align-items:center;justify-content:center;height:100vh;font-size:1.1rem}
</style></head><body>✓ Connected — launching CloudHealth…</body></html>"""


def _sftp_pull(sftp, remote_dir: str, local_dir: Path):
    import stat
    local_dir.mkdir(parents=True, exist_ok=True)
    for entry in sftp.listdir_attr(remote_dir):
        rpath = f"{remote_dir}/{entry.filename}"
        lpath = local_dir / entry.filename
        if stat.S_ISDIR(entry.st_mode): _sftp_pull(sftp, rpath, lpath)
        else: sftp.get(rpath, str(lpath))


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
            print(f"[bootstrap] Syncing {local_ver or 'none'} → {remote_ver}")
            _sftp_pull(sftp, REMOTE_ROOT, PROGRAM_DIR)
            VERSION_FILE.write_text(remote_ver)
            print("[bootstrap] Sync complete")
        else:
            print(f"[bootstrap] Version {remote_ver} up to date — skipping sync")
        return remote_ver
    finally:
        sftp.close()
        client.close()


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Show browser popup for credentials
    server = http.server.HTTPServer(("127.0.0.1", BOOTSTRAP_PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    webbrowser.open(f"http://127.0.0.1:{BOOTSTRAP_PORT}")
    _done.wait()
    server.shutdown()

    creds = _creds
    if creds and creds.get("remember"):
        save_creds(creds["host"], creds["port"], creds["username"], creds["password"])
        (CACHE_DIR/".meta").write_text(
            json.dumps({"ip": creds["host"], "user": creds["username"]}))

    print(f"[bootstrap] Connecting to {creds['host']}:{creds['port']} …")
    try:
        _sync(creds["host"], creds["port"], creds["username"], creds["password"])
    except Exception as e:
        print(f"[bootstrap] ERROR: {e}")
        sys.exit(1)

    main_py = PROGRAM_DIR / "main.py"
    if not main_py.exists():
        print(f"[bootstrap] main.py not found at {main_py}")
        sys.exit(1)

    print("[bootstrap] Launching CloudHealth…")
    subprocess.Popen([sys.executable, str(main_py)], cwd=str(PROGRAM_DIR))


if __name__ == "__main__":
    main()
