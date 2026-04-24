from core.daemon import DaemonIPC


def test_machinectl_binding() -> None:
    ipc = DaemonIPC()
    binding = ipc.get_machinectl_binding()
    assert "sudo machinectl shell sandbox@" in binding


def test_docker_wait_sequence() -> None:
    ipc = DaemonIPC()
    seq = ipc.get_docker_wait_sequence()
    assert "docker compose wait" in seq


def test_posix_acl_limits() -> None:
    ipc = DaemonIPC()
    limits = ipc.get_posix_acl_limits("/path/to/project")
    assert "setfacl -d -m u:dev:rwx /path/to/project" in limits


def test_tmux_payload_assembler() -> None:
    ipc = DaemonIPC()
    payload = ipc.assemble_tmux_payload("my_prompt")
    assert "tmux send-keys" in payload
    assert "--dangerously-skip-permissions" in payload
    assert "my_prompt" in payload
