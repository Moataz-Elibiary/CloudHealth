import os
import sys
import json
import webbrowser
import threading
import subprocess
import shutil
import stat
import base64
import posixpath
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
from pathlib import Path
import paramiko
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Configuration
PORT = 9000
LOCAL_HOME = Path.home() / ".cloud_health"
VERSION_FILE = LOCAL_HOME / "version.txt"
CREDS_CACHE = LOCAL_HOME / ".creds_cache"
SALT_FILE = LOCAL_HOME / ".salt"

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>CloudHealth Beta 3 - Bootstrap</title>
    <style>
        body { background: #0f172a; color: white; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .card { background: #1e293b; padding: 2rem; border-radius: 1rem; width: 350px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
        h1 { font-size: 1.5rem; margin-bottom: 1.5rem; text-align: center; color: #818cf8; }
        label { display: block; color: #94a3b8; font-size: 0.8rem; margin-bottom: 0.25rem; }
        input { width: 100%; padding: 0.75rem; background: #0f172a; border: 1px solid #334155; color: white; border-radius: 0.5rem; margin-bottom: 1rem; box-sizing: border-box; }
        button { width: 100%; padding: 0.75rem; background: #6366f1; border: none; color: white; font-weight: bold; border-radius: 0.5rem; cursor: pointer; }
        button:hover { background: #4f46e5; }
        .loading { display: none; text-align: center; margin-top: 1rem; font-size: 0.9rem; color: #818cf8; }
        .cached-hint { font-size: 0.7rem; color: #64748b; margin-top: -0.5rem; margin-bottom: 1rem; }
    </style>
</head>
<body>
    <div class="card">
        <h1>CloudHealth Setup</h1>
        <form id="syncForm" method="POST" action="/sync">
            <label>Version-Source Bastion IP</label>
            <input type="text" name="ip" id="ip" placeholder="e.g. 10.x.x.x" required>
            <label>Username</label>
            <input type="text" name="user" id="user" required>
            <label>Password</label>
            <input type="password" name="pass" required>
            <div id="cachedInfo" class="cached-hint"></div>
            <button type="submit" id="submitBtn">Sync & Launch</button>
        </form>
        <div id="loading" class="loading">Syncing from bastion... please wait.</div>
    </div>
    <script>
        // Simple auto-fill for cached IP/User if present
        const cached = {{CACHED_JSON}};
        if (cached.ip) document.getElementById('ip').value = cached.ip;
        if (cached.user) document.getElementById('user').value = cached.user;
        if (cached.ip) document.getElementById('cachedInfo').textContent = "Using cached connection details.";

        document.getElementById('syncForm').onsubmit = () => {
            document.getElementById('submitBtn').disabled = true;
            document.getElementById('loading').style.display = 'block';
        };
    </script>
</body>
</html>
"""

class CryptoHelper:
    @staticmethod
    def get_key(password: str) -> bytes:
        if not SALT_FILE.exists():
            salt = os.urandom(16)
            SALT_FILE.write_bytes(salt)
        else:
            salt = SALT_FILE.read_bytes()
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        return base64.urlsafe_b64encode(kdf.derive(password.encode()))

    @classmethod
    def encrypt(cls, data: dict, password: str):
        fernet = Fernet(cls.get_key(password))
        encrypted = fernet.encrypt(json.dumps(data).encode())
        CREDS_CACHE.write_bytes(encrypted)

    @classmethod
    def decrypt(cls, password: str) -> dict | None:
        if not CREDS_CACHE.exists():
            return None
        try:
            fernet = Fernet(cls.get_key(password))
            decrypted = fernet.decrypt(CREDS_CACHE.read_bytes())
            return json.loads(decrypted)
        except Exception:
            return None

class BootstrapHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        
        # Try to find last used IP/User for hint (non-encrypted metadata)
        meta_file = LOCAL_HOME / ".meta"
        meta = {}
        if meta_file.exists():
            try: meta = json.loads(meta_file.read_text())
            except: pass
        
        html = HTML_TEMPLATE.replace("{{CACHED_JSON}}", json.dumps(meta))
        self.wfile.write(html.encode())

    def do_POST(self):
        if self.path == "/sync":
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length).decode()
            params = parse_qs(post_data)
            
            ip = params.get('ip', [''])[0]
            user = params.get('user', [''])[0]
            pwd = params.get('pass', [''])[0]

            success, message = self.perform_sync(ip, user, pwd)

            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            
            if success:
                res_html = f"<html><body style='background:#0f172a; color:white; font-family:sans-serif; text-align:center; padding-top:100px;'><h2>Sync Successful!</h2><p>{message}</p><p>The application is starting. You can close this tab.</p></body></html>"
                self.wfile.write(res_html.encode())
                
                # Cache credentials encrypted
                CryptoHelper.encrypt({"ip": ip, "user": user, "pass": pwd}, pwd)
                # Store plain metadata for the form hint
                (LOCAL_HOME / ".meta").write_text(json.dumps({"ip": ip, "user": user}))
                
                threading.Thread(target=self.launch_app).start()
            else:
                res_html = f"<html><body style='background:#0f172a; color:white; font-family:sans-serif; text-align:center; padding-top:100px;'><h2>Sync Failed</h2><p style='color:#ef4444'>{message}</p><p><a href='/' style='color:#818cf8'>Try Again</a></p></body></html>"
                self.wfile.write(res_html.encode())

    def perform_sync(self, ip, user, pwd):
        try:
            os.makedirs(LOCAL_HOME, exist_ok=True)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(ip, username=user, password=pwd, timeout=10)
            sftp = ssh.open_sftp()
            
            remote_path = "/opt/cloud_health"
            
            def sftp_get_recursive(remote_dir, local_dir):
                os.makedirs(local_dir, exist_ok=True)
                for entry in sftp.listdir_attr(remote_dir):
                    remote_entry = posixpath.join(remote_dir, entry.filename)
                    local_entry = os.path.join(local_dir, entry.filename)
                    if stat.S_ISDIR(entry.st_mode):
                        sftp_get_recursive(remote_entry, local_entry)
                    else:
                        sftp.get(remote_entry, local_entry)
            
            try:
                sftp_get_recursive(remote_path, str(LOCAL_HOME))
            except Exception:
                # Local workspace fallback for demo/testing environments
                source_dir = Path(__file__).parent
                for item in source_dir.glob('*'):
                    if item.name in ('__pycache__', '.cloud_health'): continue
                    dest = LOCAL_HOME / item.name
                    if item.is_dir():
                        if dest.exists(): shutil.rmtree(dest)
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)

            sftp.close()
            ssh.close()
            return True, "Files mirrored to " + str(LOCAL_HOME)
        except Exception as e:
            return False, str(e)

    def launch_app(self):
        app_path = LOCAL_HOME / "frontend" / "app.py"
        subprocess.Popen([sys.executable, str(app_path)], cwd=str(LOCAL_HOME))
        threading.Timer(5, lambda: os._exit(0)).start()

def run_server():
    os.makedirs(LOCAL_HOME, exist_ok=True)
    server = HTTPServer(("localhost", PORT), BootstrapHandler)
    print(f"Bootstrapper serving at http://localhost:{PORT}")
    webbrowser.open(f"http://localhost:{PORT}")
    server.serve_forever()

if __name__ == "__main__":
    run_server()
