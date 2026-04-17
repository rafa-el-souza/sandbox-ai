import os
from core.hashing import generate_project_hash

def test_generate_project_hash(monkeypatch):
    """
    Test MD5 hashing of absolute paths.
    E.g. md5("/home/dev/api") -> api-8f3a9e
    """
    monkeypatch.setattr(os.path, "abspath", lambda p: "/home/dev/api")
    
    project_name = generate_project_hash("api")
    
    # "api" + "-" + md5("/home/dev/api")[:6]
    assert project_name.startswith("api-")
    assert len(project_name.split("-")[1]) == 6
