import os
import time
import subprocess
import signal
import sys
import tempfile
import socket
import json
from pathlib import Path


def runtime_dir():
    if os.name == "nt":
        return Path(tempfile.gettempdir()) / "cloud_health"
    return Path("/tmp/cloud_health")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

def test_lock_and_cleanup():
    print("Testing Backend Lock and Cleanup...", flush=True)
    lock_file = runtime_dir() / "hc.lock"
    lock_file.unlink(missing_ok=True)
    port1 = free_port()
    port2 = free_port()
    while port2 == port1:
        port2 = free_port()
    
    # 1. Start a backend instance
    proc1 = subprocess.Popen([
        sys.executable, "beta3/backend/main.py", "--port", str(port1)
    ])
    time.sleep(3) # Wait for it to start
    
    if lock_file.exists():
        print(f"PASS: Lock file created at {lock_file}", flush=True)
    else:
        print("FAIL: Lock file not created.", flush=True)
        proc1.kill()
        return

    # 2. Try to start a second instance
    print("Attempting to start second instance (should log error and not take lock)...", flush=True)
    proc2 = subprocess.Popen([
        sys.executable, "beta3/backend/main.py", "--port", str(port2)
    ])
    time.sleep(2)
    
    # Check if proc2 is still running or exited (it might stay running but shouldn't have the lock)
    # The real test is the WS 'LOCKED' message, but here we just check if it overwrote the lock.
    with open(lock_file, "r", encoding="utf-8") as f:
        payload = json.load(f)
        pid = str(payload.get("pid"))
        if pid == str(proc1.pid):
            print("PASS: First instance still holds the lock.", flush=True)
        else:
            print(f"FAIL: Second instance stole the lock (PID {pid} vs {proc1.pid})", flush=True)

    # 3. Test Cleanup on SIGTERM
    print("Terminating first instance...", flush=True)
    proc1.send_signal(signal.SIGTERM)
    time.sleep(2)
    
    if not lock_file.exists():
        print("PASS: Lock file cleaned up after termination.", flush=True)
    else:
        print("FAIL: Lock file still exists.", flush=True)

    # Cleanup proc2 if still running
    if proc2.poll() is None:
        proc2.kill()

if __name__ == "__main__":
    runtime_dir().mkdir(parents=True, exist_ok=True)
    test_lock_and_cleanup()
