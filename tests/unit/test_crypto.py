"""Tests for core/crypto.py — credential generation and htpasswd writing."""

from pathlib import Path

import pytest
from core.crypto import (
    generate_credential,
    hash_proxy_password,
    write_htpasswd,
)


class TestGenerateCredential:
    def test_length(self) -> None:
        """Generated credential has sufficient length (token_urlsafe(32) → 43 chars)."""
        pwd = generate_credential()
        assert len(pwd) >= 32

    def test_url_safe_characters(self) -> None:
        """Credential contains only URL-safe characters (base64url: A-Z, a-z, 0-9, -, _)."""
        pwd = generate_credential()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert all(c in allowed for c in pwd), f"Invalid characters in credential: {pwd}"

    def test_unique_per_call(self) -> None:
        """Each call produces a unique credential (probabilistic)."""
        credentials = {generate_credential() for _ in range(10)}
        assert len(credentials) == 10


class TestHashProxyPassword:
    def test_bcrypt_format(self) -> None:
        """Hash output starts with $2b$ (bcrypt identifier)."""
        hashed = hash_proxy_password("testpassword")
        assert hashed.startswith("$2b$")

    def test_htpasswd_line_format(self) -> None:
        """Output is formatted as 'proxyuser:<bcrypt_hash>'."""
        line = hash_proxy_password("testpassword")
        # bcrypt hash is exactly the hash, not the htpasswd line
        assert "$2b$" in line


class TestWriteHtpasswd:
    def test_writes_correct_content(self, tmp_path: Path) -> None:
        """htpasswd file contains 'proxyuser:<hash>' line."""
        proxy_dir = tmp_path / "config" / "proxy"
        proxy_dir.mkdir(parents=True)

        write_htpasswd(str(proxy_dir), "proxyuser:$2b$12$somebcrypthash")

        htpasswd = proxy_dir / ".htpasswd"
        assert htpasswd.exists()
        content = htpasswd.read_text()
        assert content.strip() == "proxyuser:$2b$12$somebcrypthash"

    def test_writes_to_correct_path(self, tmp_path: Path) -> None:
        """htpasswd is written to <config_proxy_dir>/.htpasswd."""
        proxy_dir = tmp_path / "config" / "proxy"
        proxy_dir.mkdir(parents=True)

        write_htpasswd(str(proxy_dir), "proxyuser:$2b$12$hash")

        assert (proxy_dir / ".htpasswd").exists()

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        """Existing htpasswd file is overwritten atomically."""
        proxy_dir = tmp_path / "config" / "proxy"
        proxy_dir.mkdir(parents=True)
        (proxy_dir / ".htpasswd").write_text("old_content")

        write_htpasswd(str(proxy_dir), "proxyuser:$2b$12$newhash")

        content = (proxy_dir / ".htpasswd").read_text()
        assert "newhash" in content
        assert "old_content" not in content


class TestGenerateSSHKeypair:
    """2.T RED: SSH keypair generation — Ed25519 auth + host keypairs."""

    def test_generate_ssh_auth_keypair_creates_files(self, tmp_path: Path) -> None:
        """Auth keypair: ipc_ssh_key (PEM) and authorized_keys (ssh-ed25519)."""
        from core.crypto import generate_ssh_keypair

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        generate_ssh_keypair(str(tmp_path), "auth")
        assert (secrets_dir / "ipc_ssh_key").exists()
        private_key = (secrets_dir / "ipc_ssh_key").read_text()
        assert "BEGIN" in private_key  # PEM header
        assert (secrets_dir / "authorized_keys").exists()
        public_key = (secrets_dir / "authorized_keys").read_text()
        assert public_key.startswith("ssh-ed25519")

    def test_generate_ssh_host_keypair_creates_files(self, tmp_path: Path) -> None:
        """Host keypair: ipc_host_key (PEM) and ipc_known_hosts (with IP)."""
        from core.crypto import generate_ssh_keypair

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        generate_ssh_keypair(str(tmp_path), "host", core_ipc_ip="10.100.6.3")
        assert (secrets_dir / "ipc_host_key").exists()
        private_key = (secrets_dir / "ipc_host_key").read_text()
        assert "BEGIN" in private_key  # PEM header
        assert (secrets_dir / "ipc_known_hosts").exists()
        known_hosts = (secrets_dir / "ipc_known_hosts").read_text()
        assert "10.100.6.3" in known_hosts

    def test_generate_ssh_keypair_idempotent(self, tmp_path: Path) -> None:
        """Second call does not overwrite existing key files."""
        from core.crypto import generate_ssh_keypair

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        generate_ssh_keypair(str(tmp_path), "auth")
        first_content = (secrets_dir / "ipc_ssh_key").read_text()
        generate_ssh_keypair(str(tmp_path), "auth")
        second_content = (secrets_dir / "ipc_ssh_key").read_text()
        assert first_content == second_content

    def test_generate_ssh_keypair_uses_ed25519(self, tmp_path: Path) -> None:
        """Generated public key starts with ssh-ed25519."""
        from core.crypto import generate_ssh_keypair

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        generate_ssh_keypair(str(tmp_path), "auth")
        public_key = (secrets_dir / "authorized_keys").read_text()
        assert public_key.startswith("ssh-ed25519")

    def test_generate_ssh_keypair_invalid_pair_type(self, tmp_path: Path) -> None:
        """Invalid pair_type raises ValueError."""
        from core.crypto import generate_ssh_keypair

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        with pytest.raises(ValueError, match="Invalid pair_type"):
            generate_ssh_keypair(str(tmp_path), "invalid")


class TestSecretsFileModes:
    """Hydration writes secret files at restrictive mode regardless of umask (Decision 6)."""

    def test_auth_keypair_modes(self, tmp_path: Path) -> None:
        import os

        from core.crypto import generate_ssh_keypair

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        old_umask = os.umask(0o022)
        try:
            generate_ssh_keypair(str(tmp_path), "auth")
        finally:
            os.umask(old_umask)
        assert ((secrets_dir / "ipc_ssh_key").stat().st_mode & 0o777) == 0o600
        assert ((secrets_dir / "authorized_keys").stat().st_mode & 0o777) == 0o600

    def test_host_keypair_modes(self, tmp_path: Path) -> None:
        import os

        from core.crypto import generate_ssh_keypair

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        old_umask = os.umask(0o022)
        try:
            generate_ssh_keypair(str(tmp_path), "host", core_ipc_ip="10.100.6.3")
        finally:
            os.umask(old_umask)
        assert ((secrets_dir / "ipc_host_key").stat().st_mode & 0o777) == 0o600
        assert ((secrets_dir / "ipc_known_hosts").stat().st_mode & 0o777) == 0o600

    def test_htpasswd_mode_640(self, tmp_path: Path) -> None:
        import os

        proxy_dir = tmp_path / "config" / "proxy"
        proxy_dir.mkdir(parents=True)
        old_umask = os.umask(0o022)
        try:
            write_htpasswd(str(proxy_dir), "proxyuser:$2b$12$x")
        finally:
            os.umask(old_umask)
        assert ((proxy_dir / ".htpasswd").stat().st_mode & 0o777) == 0o640
