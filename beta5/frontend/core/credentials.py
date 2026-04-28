"""
Credential manager.
Encrypts / decrypts the cached version-source bastion credentials using
Fernet symmetric encryption (from the `cryptography` library — already a
transitive dependency of paramiko, so zero extra installation required).

Key derivation: PBKDF2-HMAC-SHA256 from the user-supplied password.
Salt: stored alongside the ciphertext in the cache file.

Cache file: ~/Documents/cloud_health/credentials.cache
"""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


CACHE_DIR  = Path.home() / "Documents" / "cloud_health"
CACHE_FILE = CACHE_DIR / "credentials.cache"
PBKDF2_ITERATIONS = 480_000   # OWASP 2023 recommendation


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm  = hashes.SHA256(),
        length     = 32,
        salt       = salt,
        iterations = PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def save_credentials(host: str, port: int, username: str, password: str) -> None:
    """Encrypt and cache version-source bastion credentials."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    salt    = os.urandom(16)
    key     = _derive_key(password, salt)
    fernet  = Fernet(key)
    payload = json.dumps({"host": host, "port": port,
                          "username": username, "password": password}).encode()
    ciphertext = fernet.encrypt(payload)
    CACHE_FILE.write_bytes(
        base64.b64encode(salt) + b"\n" + ciphertext
    )
    # Restrict permissions on non-Windows
    try:
        os.chmod(CACHE_FILE, 0o600)
    except Exception:
        pass


def load_credentials(password: str) -> Optional[dict]:
    """
    Decrypt cached credentials using the supplied password.
    Returns dict with host/port/username/password, or None if decryption fails.
    """
    if not CACHE_FILE.exists():
        return None
    try:
        raw   = CACHE_FILE.read_bytes().split(b"\n", 1)
        salt  = base64.b64decode(raw[0])
        ciph  = raw[1]
        key   = _derive_key(password, salt)
        fernet= Fernet(key)
        data  = json.loads(fernet.decrypt(ciph).decode())
        return data
    except Exception:
        return None   # wrong password or corrupted cache


def clear_credentials() -> None:
    CACHE_FILE.unlink(missing_ok=True)


def credentials_cached() -> bool:
    return CACHE_FILE.exists()
