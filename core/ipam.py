import fcntl
import json
import os
from typing import Dict, Tuple

class IPAMLockException(BlockingIOError):
    pass

def acquire_locks(global_lock_path: str, local_lock_path: str, fail_mock: bool = False) -> Tuple[int, int]:
    """
    Acquire dual fcntl locks.
    """
    if fail_mock:
        raise IPAMLockException("Mock failure")
        
    try:
        os.makedirs(os.path.dirname(global_lock_path), exist_ok=True)
        # For local lock it could just be in a temp dir or same dir
        os.makedirs(os.path.dirname(local_lock_path) or ".", exist_ok=True)
        
        global_fd = os.open(global_lock_path, os.O_CREAT | os.O_RDWR)
        local_fd = os.open(local_lock_path, os.O_CREAT | os.O_RDWR)
        
        fcntl.flock(global_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(local_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return global_fd, local_fd
    except BlockingIOError:
        raise IPAMLockException("Could not acquire locks")

def _read_ledger(ledger_path: str) -> Dict[str, str]:
    if not os.path.exists(ledger_path):
        return {}
    with open(ledger_path, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def _write_ledger(ledger_path: str, data: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    with open(ledger_path, 'w') as f:
        json.dump(data, f, indent=2)

def get_next_subnet(global_ledger_path: str, local_lock_path: str, project_id: str) -> str:
    """
    Allocate the next /16 subnet dynamically.
    """
    global_lock = str(global_ledger_path) + ".lock"
    
    global_fd, local_fd = acquire_locks(global_lock, str(local_lock_path))
    
    try:
        ledger = _read_ledger(str(global_ledger_path))
        
        if project_id in ledger:
            return ledger[project_id]
        
        existing_subnets = list(ledger.values())
        if not existing_subnets:
            next_subnet = "10.100.0.0/16"
        else:
            highest = 100
            for sub in existing_subnets:
                parts = sub.split('.')
                if len(parts) > 1 and parts[1].isdigit():
                    val = int(parts[1])
                    if val > highest:
                        highest = val
            next_subnet = f"10.{highest + 1}.0.0/16"
            
        ledger[project_id] = next_subnet
        _write_ledger(str(global_ledger_path), ledger)
        return next_subnet
    finally:
        os.close(global_fd)
        os.close(local_fd)
