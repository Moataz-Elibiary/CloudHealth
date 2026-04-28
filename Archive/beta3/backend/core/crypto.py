import base64
import os
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

class CredentialCrypto:
    """Handles encryption and decryption of bastion credentials using a user-provided password."""
    
    def __init__(self, password: str):
        # Derive a key from the password
        salt = b'cloud_health_salt'  # In a real app, this should be unique and stored, but for a local cache it's fine
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        self.fernet = Fernet(key)

    def encrypt(self, data: str) -> str:
        """Encrypts a string and returns a base64 encoded string."""
        return self.fernet.encrypt(data.encode()).decode()

    def decrypt(self, encrypted_data: str) -> str:
        """Decrypts an encrypted string."""
        try:
            return self.fernet.decrypt(encrypted_data.encode()).decode()
        except Exception:
            raise ValueError("Decryption failed. Incorrect password or corrupted data.")

def encrypt_file(file_path: str, password: str, data: str):
    """Utility to encrypt and save data to a file."""
    crypto = CredentialCrypto(password)
    encrypted = crypto.encrypt(data)
    with open(file_path, 'w') as f:
        f.write(encrypted)

def decrypt_file(file_path: str, password: str) -> str:
    """Utility to read and decrypt data from a file."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Credential file not found: {file_path}")
    with open(file_path, 'r') as f:
        encrypted = f.read()
    crypto = CredentialCrypto(password)
    return crypto.decrypt(encrypted)
