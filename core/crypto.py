"""Proxy credential generation: password, bcrypt hash, htpasswd file.

Generates a cryptographically secure proxy password, hashes it via bcrypt,
and writes the htpasswd file for Squid proxy authentication.
"""

import os
import secrets

import bcrypt


def generate_proxy_password() -> str:
    """Generate a cryptographically secure URL-safe proxy password.

    Uses secrets.token_urlsafe(32) producing a 43-character base64url string.
    """
    return secrets.token_urlsafe(32)


def hash_proxy_password(password: str) -> str:
    """Hash a proxy password using bcrypt.

    Returns the raw bcrypt hash string (e.g., '$2b$12$...').
    """
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("ascii")


def write_htpasswd(config_proxy_dir: str, htpasswd_line: str) -> None:
    """Write the htpasswd file atomically to <config_proxy_dir>/.htpasswd.

    Overwrites any existing file. The htpasswd_line should be
    formatted as 'proxyuser:<bcrypt_hash>'.
    """
    htpasswd_path = os.path.join(config_proxy_dir, ".htpasswd")
    tmp_path = htpasswd_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(htpasswd_line + "\n")
    os.replace(tmp_path, htpasswd_path)
