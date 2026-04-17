import secrets
import string
import bcrypt
from typing import Dict

def generate_random_password(length: int = 32) -> str:
    """Generate a cryptographically secure random 32-character proxy sequence."""
    chars = string.ascii_letters + string.digits + "!@#$%^&*()"
    return "".join(secrets.choice(chars) for _ in range(length))

def generate_proxy_hash(password: str) -> str:
    """
    Establish pure-Python bcrypt syntax hashes.
    """
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('ascii')

def assemble_env_mapping(password: str, hashed: str) -> Dict[str, str]:
    """
    Assemble the variable execution arrays dynamically mapping to .sandbox/configs/ .
    """
    return {
        "SANDBOX_PROXY_PASSWORD": password,
        "SANDBOX_PROXY_HASH": hashed
    }
