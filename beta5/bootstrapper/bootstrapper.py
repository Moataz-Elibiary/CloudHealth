#!/usr/bin/env python3
"""
CloudHealth Bootstrapper.
Compiled to a single exe with PyInstaller — never changes after first release.
Steps: try cached creds → SSH version check → SFTP sync if needed → launch main.py
On auth failure: re-prompt user (up to MAX_ATTEMPTS times), then exit with error.
"""
import hashlib, http.server, json, os, subprocess, sys, threading, webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

USER_DATA_DIR  = Path.home() / "Documents" / "cloud_health"
CACHE_DIR      = USER_DATA_DIR
CACHE_FILE     = CACHE_DIR / "credentials.cache"
SALT_FILE      = CACHE_DIR / ".salt"
VERSION_FILE   = CACHE_DIR / "version.txt"
PROGRAM_DIR    = CACHE_DIR / "program"
BOOTSTRAP_PORT = 9000
REMOTE_ROOT    = "/opt/cloud_health"
REMOTE_VERSION = f"{REMOTE_ROOT}/version.txt"
MAX_ATTEMPTS   = 3


# ---------------------------------------------------------------------------
# Credential cache — encrypted with a machine-derived key so cached creds
# can be loaded on startup without requiring the user to type their password.
# ---------------------------------------------------------------------------

def _get_salt() -> bytes:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if SALT_FILE.exists():
        return SALT_FILE.read_bytes()
    salt = os.urandom(32)
    SALT_FILE.write_bytes(salt)
    try: os.chmod(SALT_FILE, 0o600)
    except Exception: pass
    return salt


def _machine_key() -> bytes:
    import base64
    return base64.urlsafe_b64encode(hashlib.sha256(_get_salt()).digest())


def save_creds(host: str, port: int, username: str, password: str) -> None:
    from cryptography.fernet import Fernet
    ciph = Fernet(_machine_key()).encrypt(
        json.dumps({"host": host, "port": port,
                    "username": username, "password": password}).encode()
    )
    CACHE_FILE.write_bytes(ciph)
    try: os.chmod(CACHE_FILE, 0o600)
    except Exception: pass


def load_creds() -> Optional[dict]:
    from cryptography.fernet import Fernet
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(Fernet(_machine_key()).decrypt(CACHE_FILE.read_bytes()).decode())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTTP popup — single server instance reused across all retry attempts.
# The "Connecting…" page polls /status so the form can show inline errors.
# ---------------------------------------------------------------------------

_creds: Optional[dict] = None
_done  = threading.Event()
_error_msg: str = ""
_conn_status: dict = {"state": "pending"}   # "pending" | "error" | "fatal" | "ok"


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        if self.path == "/status":
            body = json.dumps(_conn_status).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            meta = {}
            meta_file = CACHE_DIR / ".meta"
            if meta_file.exists():
                try: meta = json.loads(meta_file.read_text())
                except: pass
            self.send_response(200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.end_headers()
            self.wfile.write(_html(meta, _error_msg).encode())

    def do_POST(self):
        global _creds, _conn_status
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length).decode()
        params = {k: v[0] for k, v in parse_qs(body).items()}
        _creds = {
            "host":     params.get("host", "").strip(),
            "port":     int(params.get("port", "22") or "22"),
            "username": params.get("user", "").strip(),
            "password": params.get("pass", "").strip(),
            "remember": params.get("remember", "") == "on",
        }
        _conn_status = {"state": "pending"}
        self.send_response(200)
        self.send_header("Content-Type", "text/html;charset=utf-8")
        self.end_headers()
        self.wfile.write(_connecting_html().encode())
        threading.Thread(target=_done.set, daemon=True).start()


def _html(meta: dict, error: str = "") -> str:
    ip   = meta.get("ip", "")
    user = meta.get("user", "")
    error_html = f'<div class="err">{error}</div>' if error else ""
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
.err{{background:#3b1a1a;border:1px solid #7f1d1d;border-radius:5px;color:#f87171;
      font-size:.82rem;padding:.65rem .85rem;margin-bottom:1rem}}
</style></head><body><div class="card">
<h1>⚡ CloudHealth Setup</h1>
{error_html}
<form method="POST">
  <label>Version-Source Bastion IP</label>
  <input name="host" value="{ip}" placeholder="10.x.x.x" required>
  <div class="row2">
    <div><label>Username</label><input name="user" value="{user}" required></div>
    <div><label>Port</label><input name="port" value="22" type="number"></div>
  </div>
  <label>Password</label><input name="pass" type="password" required>
  <div class="chk"><input type="checkbox" name="remember" checked id="r">
    <label for="r" style="margin-bottom:0;text-transform:none">Remember credentials</label></div>
  <button type="submit">Connect &amp; Launch →</button>
</form></div></body></html>"""


def _connecting_html() -> str:
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
body{font-family:system-ui;background:#0f172a;color:#e8edf8;
     display:flex;align-items:center;justify-content:center;height:100vh;font-size:1.1rem}
.msg{text-align:center;line-height:1.7}
.ok{color:#22c55e} .err{color:#f87171}
</style></head><body><div class="msg" id="m">⏳ Connecting…</div>
<script>
(function poll(){
  fetch('/status').then(r=>r.json()).then(s=>{
    if(s.state==='pending'){
      if(s.msg) document.getElementById('m').textContent='⏳ '+s.msg;
      setTimeout(poll,1500);return;
    }
    if(s.state==='ok'){
      document.getElementById('m').className='msg ok';
      document.getElementById('m').textContent='✓ Connected — launching CloudHealth…';
      return;
    }
    if(s.state==='fatal'){
      document.getElementById('m').className='msg err';
      document.getElementById('m').innerHTML='✗ '+s.msg;
      return;
    }
    // error — redirect back to form (which now shows the error message)
    window.location='/';
  }).catch(()=>{
    // server shut down after success
    document.getElementById('m').className='msg ok';
    document.getElementById('m').textContent='✓ Connected — launching CloudHealth…';
  });
})();
</script></body></html>"""


# ---------------------------------------------------------------------------
# SSH / SFTP helpers
# ---------------------------------------------------------------------------

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


def _install_deps(program_dir: Path) -> None:
    req_file   = program_dir / "requirements.txt"
    vendor_dir = program_dir / "vendor"
    if not req_file.exists():
        raise RuntimeError("requirements.txt missing from synced program — source server is misconfigured")
    if not vendor_dir.exists():
        raise RuntimeError("vendor/ directory missing from synced program — source server must bundle wheels with 'pip download'")
    print("[bootstrap] Installing dependencies from vendor/ (offline) …")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install",
         "--no-index", "--find-links", str(vendor_dir),
         "-r", str(req_file), "--quiet"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pip install failed:\n{result.stderr.strip()}")
    print("[bootstrap] Dependencies installed")


def _sync(host: str, port: int, username: str, password: str) -> tuple:
    """Returns (remote_version, synced) where synced=True if files were pulled."""
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
            return remote_ver, True
        else:
            print(f"[bootstrap] Version {remote_ver} up to date — skipping sync")
            return remote_ver, False
    finally:
        sftp.close()
        client.close()


def _launch():
    main_py = PROGRAM_DIR / "main.py"
    if not main_py.exists():
        print(f"[bootstrap] main.py not found at {main_py}")
        sys.exit(1)
    print("[bootstrap] Launching CloudHealth…")
    subprocess.Popen([sys.executable, str(main_py)], cwd=str(PROGRAM_DIR))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global _error_msg, _conn_status, _done
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # --- Try cached credentials first (no popup) ---
    cached = load_creds()
    if cached:
        print(f"[bootstrap] Trying cached credentials for {cached['host']}:{cached['port']} …")
        try:
            _, synced = _sync(cached["host"], cached["port"], cached["username"], cached["password"])
            if synced:
                _install_deps(PROGRAM_DIR)
            _launch()
            return
        except Exception as e:
            print(f"[bootstrap] Cached credentials failed: {e} — clearing cache")
            CACHE_FILE.unlink(missing_ok=True)
            _error_msg = f"Saved credentials failed: {e}. Please enter your credentials again."

    # --- Credential popup with retry loop ---
    server = http.server.HTTPServer(("127.0.0.1", BOOTSTRAP_PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webbrowser.open(f"http://127.0.0.1:{BOOTSTRAP_PORT}")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        _done.wait()
        _done.clear()
        creds = _creds

        print(f"[bootstrap] Connecting to {creds['host']}:{creds['port']} …"
              f" (attempt {attempt}/{MAX_ATTEMPTS})")
        try:
            _, synced = _sync(creds["host"], creds["port"], creds["username"], creds["password"])
            if synced:
                _conn_status = {"state": "pending", "msg": "Installing dependencies…"}
                _install_deps(PROGRAM_DIR)
        except Exception as e:
            msg = str(e)
            print(f"[bootstrap] Connection failed: {msg}")
            if attempt == MAX_ATTEMPTS:
                print("[bootstrap] Max attempts reached — exiting")
                _conn_status = {"state": "fatal",
                                "msg": f"Connection failed after {MAX_ATTEMPTS} attempts: {msg}"}
                import time; time.sleep(3)   # let JS pick up the fatal status
                server.shutdown()
                sys.exit(1)
            remaining = MAX_ATTEMPTS - attempt
            _conn_status = {"state": "error", "msg": msg}
            _error_msg = f"Login failed: {msg}. {remaining} attempt(s) remaining."
            print(f"[bootstrap] Retrying ({remaining} attempt(s) remaining) …")
            continue

        # Success — save credentials only after a confirmed working connection
        if creds.get("remember"):
            save_creds(creds["host"], creds["port"], creds["username"], creds["password"])
            (CACHE_DIR / ".meta").write_text(
                json.dumps({"ip": creds["host"], "user": creds["username"]}))

        _conn_status = {"state": "ok"}
        import time; time.sleep(1)   # let JS pick up the ok status
        server.shutdown()
        _launch()
        return


if __name__ == "__main__":
    main()
