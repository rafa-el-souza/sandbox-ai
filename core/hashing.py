import hashlib
import os


def generate_project_hash(project_dir: str) -> str:
    """Generate deterministic project hash from absolute path."""
    abs_path = os.path.abspath(project_dir)
    hash_str = hashlib.md5(abs_path.encode('utf-8')).hexdigest()
    base = os.path.basename(abs_path)
    return f"{base}-{hash_str[:6]}"
