"""
Version sync module.
Compares local frontend version with the version-source bastion.
SFTPs the entire program directory if versions differ.
Also checks each cluster bastion's backend version and pushes if needed
(this is handled by tunnel_manager during tunnel setup).
"""
import logging
import os
import sys
from pathlib import Path
from typing import Tuple

import paramiko

log = logging.getLogger("frontend.version_sync")

LOCAL_DIR          = Path(__file__).parent.parent.parent   # root of the program
LOCAL_VERSION_FILE = LOCAL_DIR / "version.txt"
REMOTE_ROOT        = "/opt/cloud_health"
REMOTE_VERSION     = f"{REMOTE_ROOT}/version.txt"


def get_local_version() -> str:
    if LOCAL_VERSION_FILE.exists():
        return LOCAL_VERSION_FILE.read_text().strip()
    return "0.0.0"


def check_and_sync(
    host: str, port: int, username: str, password: str, ssh_timeout: int = 30
) -> Tuple[bool, str]:
    """
    Connect to version-source bastion.
    Returns (synced: bool, version: str).
    Raises on connection failure (caller shows error and exits).
    """
    log.info(f"Connecting to version-source bastion {host}:{port}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host, port=port, username=username, password=password,
        timeout=ssh_timeout, banner_timeout=ssh_timeout, auth_timeout=ssh_timeout,
    )

    sftp = client.open_sftp()
    try:
        # Read remote version
        try:
            with sftp.open(REMOTE_VERSION) as fh:
                remote_ver = fh.read().decode().strip()
        except IOError:
            raise RuntimeError(
                f"version.txt not found at {REMOTE_VERSION} on version-source bastion. "
                f"Check that the program is deployed correctly.")

        local_ver = get_local_version()
        log.info(f"Local version: {local_ver}  Remote version: {remote_ver}")

        if local_ver == remote_ver:
            log.info("Versions match — no sync needed")
            return False, remote_ver

        # Versions differ — pull everything from version-source
        log.info(f"Syncing from version-source ({local_ver} → {remote_ver})")
        _sftp_pull_dir(sftp, REMOTE_ROOT, LOCAL_DIR)
        log.info("Sync complete")
        return True, remote_ver

    finally:
        sftp.close()
        client.close()


def _sftp_pull_dir(sftp, remote_dir: str, local_dir: Path):
    """Recursively pull remote_dir → local_dir."""
    local_dir.mkdir(parents=True, exist_ok=True)
    try:
        entries = sftp.listdir_attr(remote_dir)
    except IOError:
        return
    import stat
    for entry in entries:
        remote_path = f"{remote_dir}/{entry.filename}"
        local_path  = local_dir / entry.filename
        if stat.S_ISDIR(entry.st_mode):
            _sftp_pull_dir(sftp, remote_path, local_path)
        else:
            log.debug(f"SFTP get {remote_path} → {local_path}")
            sftp.get(remote_path, str(local_path))
