"""
Beta4 core/crypto.py — fixed credential encryption.

Bug fixed vs Beta3: hardcoded salt b'cloud_health_salt' replaced with
a random 16-byte salt stored in SALT_FILE. Without this, all encrypted
files share the same salt, enabling rainbow table precomputation.

480,000 PBKDF2-SHA256 iterations (OWASP 2023 recommendation).
"""
from __future__ import annotations
import base64, os
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_RUNTIME_DIR = Path(os.environ.get("CLOUD_HEALTH_RUNTIME_DIR",
                                    "/tmp/cloud_health"))
_SALT_FILE   = _RUNTIME_DIR / ".crypto_salt"
_ITERATIONS  = 480_000


def _get_or_create_salt() -> bytes:
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if _SALT_FILE.exists():
        return _SALT_FILE.read_bytes()
    salt = os.urandom(16)
    _SALT_FILE.write_bytes(salt)
    try:
        os.chmod(_SALT_FILE, 0o600)
    except Exception:
        pass
    return salt


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm  = hashes.SHA256(),
        length     = 32,
        salt       = salt,
        iterations = _ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


class CredentialCrypto:
    def __init__(self, password: str):
        salt = _get_or_create_salt()
        self.fernet = Fernet(_derive_key(password, salt))

    def encrypt(self, data: str) -> str:
        return self.fernet.encrypt(data.encode()).decode()

    def decrypt(self, encrypted_data: str) -> str:
        try:
            return self.fernet.decrypt(encrypted_data.encode()).decode()
        except Exception:
            raise ValueError("Decryption failed — wrong password or corrupted data.")


def encrypt_file(file_path: str, password: str, data: str):
    crypto = CredentialCrypto(password)
    with open(file_path, "w") as f:
        f.write(crypto.encrypt(data))


def decrypt_file(file_path: str, password: str) -> str:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Credential file not found: {file_path}")
    with open(file_path) as f:
        return CredentialCrypto(password).decrypt(f.read())
