import pytest
from core.ipam import get_next_subnet, acquire_locks

def test_ipam_sequential_allocation(tmp_path):
    """
    Test sequential /16 bounding arrays.
    """
    global_lock_path = tmp_path / "ipam.json"
    local_lock_path = tmp_path / "state.lock"
    
    # First allocation: should be 10.100.0.0/16
    subnet_1 = get_next_subnet(global_lock_path, local_lock_path, "project-1")
    assert subnet_1 == "10.100.0.0/16"
    
    # Second allocation: should be 10.101.0.0/16
    subnet_2 = get_next_subnet(global_lock_path, local_lock_path, "project-2")
    assert subnet_2 == "10.101.0.0/16"
    
    # Retrieving an existing project should return the same subnet
    subnet_1_again = get_next_subnet(global_lock_path, local_lock_path, "project-1")
    assert subnet_1_again == "10.100.0.0/16"

def test_ipam_dual_fcntl_locks():
    """
    Test dual fcntl locks logic. It should raise an exception if locked.
    """
    with pytest.raises(BlockingIOError):
        # acquire_locks should throw if it can't get both locks
        # we will mock the lock failure in the actual test later or simulate
        acquire_locks("global.lock", "local.lock", fail_mock=True)

def test_ipam_fcntl_blocking_io_error(tmp_path):
    global_lock_path = tmp_path / "ipam.json"
    local_lock_path = tmp_path / "state.lock"
    
    from unittest.mock import patch
    with patch("fcntl.flock") as mock_flock:
        mock_flock.side_effect = BlockingIOError(11, "Resource temporarily unavailable")
        from core.ipam import IPAMLockException
        with pytest.raises(IPAMLockException, match="Could not acquire locks"):
            acquire_locks(str(global_lock_path), str(local_lock_path))

def test_ipam_json_decode_error(tmp_path):
    global_lock_path = tmp_path / "ipam.json"
    local_lock_path = tmp_path / "state.lock"
    
    with open(global_lock_path, "w") as f:
        f.write("{ bad json }")
        
    subnet = get_next_subnet(str(global_lock_path), str(local_lock_path), "project-error")
    assert subnet == "10.100.0.0/16"

def test_ipam_high_subnet_increment(tmp_path):
    global_lock_path = tmp_path / "ipam.json"
    local_lock_path = tmp_path / "state.lock"
    
    import json
    with open(global_lock_path, "w") as f:
        json.dump({"project-alpha": "10.105.0.0/16"}, f)
        
    subnet = get_next_subnet(str(global_lock_path), str(local_lock_path), "project-beta")
    assert subnet == "10.106.0.0/16"
