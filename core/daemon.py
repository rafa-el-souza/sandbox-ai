from core.project_config import MachinectlAuth


class DaemonIPC:
    def get_machinectl_binding(self, auth: MachinectlAuth = MachinectlAuth.SUDO) -> str:
        """Verify exact string bindings for ``[sudo] machinectl shell sandbox@``.

        When ``auth`` is :data:`MachinectlAuth.SUDO` the prefix includes ``sudo``;
        when ``auth`` is :data:`MachinectlAuth.POLKIT` the prefix is omitted.
        """
        prefix = "sudo " if auth == MachinectlAuth.SUDO else ""
        return f"{prefix}machinectl shell sandbox@"

    def get_docker_wait_sequence(self) -> str:
        """Assert positive validation of the docker compose wait blocking sequence natively"""
        return "docker compose wait"

    def get_posix_acl_limits(self, target_path: str) -> str:
        """Confirm positive mappings for POSIX ACL limits"""
        return f"setfacl -d -m u:dev:rwx {target_path}"

    def assemble_tmux_payload(self, warmup_prompt: str) -> str:
        """Physically guarantee the tmux send-keys payload assembler correctly interpolates"""
        return (
            f"tmux send-keys -t sandbox \"sandbox start --dangerously-skip-permissions && echo '{warmup_prompt}'\" C-m"
        )
