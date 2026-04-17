import os
from typing import Dict, Any

class YAMLCompiler:
    def extract_meta(self, yaml_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Extract x-sandbox-meta and remove it from the tree."""
        if "x-sandbox-meta" in yaml_dict:
            meta = yaml_dict.pop("x-sandbox-meta")
            return meta
        return {}

    def generate_network_block(self) -> Dict[str, Any]:
        """Maps DMZ trap bounds (proxy_net)."""
        return {
            "proxy_net": {
                "external": True
            }
        }
        
    def graft_caddy_loopback(self, service: Dict[str, Any], ip: str) -> None:
        """Mathematically grafts Caddy loopbacks (caddy.listen) into labels."""
        if "labels" not in service:
            service["labels"] = []
        service["labels"].append(f"caddy.listen={ip}:443")
        
    def format_telemetry_volumes(self, service: Dict[str, Any], project_dir: str) -> None:
        """Deeply formats local host volume mappings securely."""
        if "volumes" not in service:
            service["volumes"] = []
            
        admin_zsh = os.path.join(project_dir, ".sandbox/logs/admin/.zsh_history")
        admin_bash = os.path.join(project_dir, ".sandbox/logs/admin/.bash_history")
        
        service["volumes"].append(f"{admin_bash}:/home/dev/.bash_history")
        service["volumes"].append(f"{admin_zsh}:/home/dev/.zsh_history")
