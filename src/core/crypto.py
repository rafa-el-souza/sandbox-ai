# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Proxy credential generation: password, bcrypt hash, htpasswd file.

Generates a cryptographically secure proxy password, hashes it via bcrypt,
and writes the htpasswd file for Squid proxy authentication.
"""

import os
import secrets

import bcrypt

from core.hydration import RESTRICTIVE_RO_MODE, RESTRICTIVE_SECRET_MODE, write_restricted


def generate_credential() -> str:
    """Generate a cryptographically secure URL-safe credential string.

    Uses secrets.token_urlsafe(32) producing a 43-character base64url string.
    Credential-type-agnostic: used for proxy passwords, PG_PASSWORD, etc.
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
    write_restricted(tmp_path, htpasswd_line + "\n", RESTRICTIVE_RO_MODE)
    os.replace(tmp_path, htpasswd_path)


def generate_ssh_keypair(
    instance_dir: str,
    pair_type: str,
    *,
    core_ipc_ip: str = "",
) -> None:
    """Generate an Ed25519 SSH keypair for IPC transport.

    Args:
        instance_dir: Instance directory (keypairs written to secrets/ subdirectory).
        pair_type: Either "auth" (admin→core) or "host" (core identity).
        core_ipc_ip: Required for pair_type="host" — used in known_hosts entry.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    secrets_dir = os.path.join(instance_dir, "secrets")

    if pair_type == "auth":
        private_key_path = os.path.join(secrets_dir, "ipc_ssh_key")
        public_key_path = os.path.join(secrets_dir, "authorized_keys")
    elif pair_type == "host":
        private_key_path = os.path.join(secrets_dir, "ipc_host_key")
        public_key_path = os.path.join(secrets_dir, "ipc_known_hosts")
    else:
        raise ValueError(f"Invalid pair_type: {pair_type!r} (expected 'auth' or 'host')")

    # Idempotency: skip if private key already exists
    if os.path.exists(private_key_path):
        return

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    # Write private key (PEM, no encryption)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    write_restricted(private_key_path, private_pem, RESTRICTIVE_SECRET_MODE)

    # Write public key / known_hosts
    public_ssh = public_key.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )

    if pair_type == "auth":
        # authorized_keys: ssh-ed25519 <base64> format (already in OpenSSH format)
        write_restricted(public_key_path, public_ssh + b"\n", RESTRICTIVE_SECRET_MODE)
    else:
        # known_hosts: <ip> ssh-ed25519 <base64>
        # Extract just the base64 portion from the OpenSSH public key
        parts = public_ssh.decode("ascii").split()
        key_type = parts[0]
        key_data = parts[1]
        known_hosts_line = f"{core_ipc_ip} {key_type} {key_data}\n"
        write_restricted(public_key_path, known_hosts_line, RESTRICTIVE_SECRET_MODE)
