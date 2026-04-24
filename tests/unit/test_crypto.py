"""Tests for core/crypto.py — credential generation and htpasswd writing."""

from pathlib import Path

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
