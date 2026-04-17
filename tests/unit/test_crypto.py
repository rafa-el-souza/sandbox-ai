from core.crypto import generate_random_password, generate_proxy_hash, assemble_env_mapping

def test_generate_random_password():
    pwd = generate_random_password()
    assert len(pwd) == 32

def test_generate_proxy_hash():
    # It must format proxy hashes properly. Could be bcrypt.
    pwd = "testpassword"
    hashed = generate_proxy_hash(pwd)
    # the exact bcrypt format validation
    assert hashed.startswith("$2")

def test_assemble_env_mapping():
    mapping = assemble_env_mapping("my_proxy_pwd", "my_proxy_hash")
    assert "SANDBOX_PROXY_PASSWORD" in mapping
    assert mapping["SANDBOX_PROXY_PASSWORD"] == "my_proxy_pwd"
    assert "SANDBOX_PROXY_HASH" in mapping
    assert mapping["SANDBOX_PROXY_HASH"] == "my_proxy_hash"
