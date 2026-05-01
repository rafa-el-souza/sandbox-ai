from core.daemon import DaemonIPC
from core.project_config import MachinectlAuth


def test_machinectl_binding_default_sudo() -> None:
    ipc = DaemonIPC()
    binding = ipc.get_machinectl_binding()
    assert binding == "sudo machinectl shell sandbox@"


def test_machinectl_binding_sudo_explicit() -> None:
    ipc = DaemonIPC()
    binding = ipc.get_machinectl_binding(MachinectlAuth.SUDO)
    assert binding == "sudo machinectl shell sandbox@"


def test_machinectl_binding_polkit_omits_sudo() -> None:
    ipc = DaemonIPC()
    binding = ipc.get_machinectl_binding(MachinectlAuth.POLKIT)
    assert binding == "machinectl shell sandbox@"
    assert "sudo" not in binding


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
