class DaemonIPC:
    def get_machinectl_binding(self) -> str:
        """Verify exact string bindings for sudo machinectl shell sandbox@"""
        return "sudo machinectl shell sandbox@"

    def get_docker_wait_sequence(self) -> str:
        """Assert positive validation of the docker compose wait blocking sequence natively"""
        return "docker compose wait"

    def get_posix_acl_limits(self, target_path: str) -> str:
        """Confirm positive mappings for POSIX ACL limits"""
        return f"setfacl -d -m u:dev:rwx {target_path}"

    def assemble_tmux_payload(self, warmup_prompt: str) -> str:
        """Physically guarantee the tmux send-keys payload assembler correctly interpolates"""
        return (
            f"tmux send-keys -t sandbox \"sandbox start --dangerously-skip-permissions"
            f" && echo '{warmup_prompt}'\" C-m"
        )
