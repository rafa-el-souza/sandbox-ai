from core.compose import YAMLCompiler

def test_extract_sandbox_meta():
    yaml_dict = {
        "x-sandbox-meta": {
            "version": "1.0"
        },
        "services": {}
    }
    compiler = YAMLCompiler()
    meta = compiler.extract_meta(yaml_dict)
    assert meta["version"] == "1.0"
    assert "x-sandbox-meta" not in yaml_dict

def test_map_dmz_trap_bounds():
    compiler = YAMLCompiler()
    network_block = compiler.generate_network_block()
    assert "proxy_net" in network_block
    assert network_block["proxy_net"]["external"] is True

def test_graft_caddy_loopbacks():
    compiler = YAMLCompiler()
    service = {"labels": ["some.label=true"]}
    ip = "127.0.1.5"
    compiler.graft_caddy_loopback(service, ip)
    assert f"caddy.listen={ip}:443" in service["labels"]

def test_format_host_volume_mappings():
    compiler = YAMLCompiler()
    project_dir = "/home/user/myproject"
    service = {"volumes": []}
    compiler.format_telemetry_volumes(service, project_dir)
    assert f"{project_dir}/.sandbox/logs/admin/.bash_history:/home/dev/.bash_history" in service["volumes"]
    assert f"{project_dir}/.sandbox/logs/admin/.zsh_history:/home/dev/.zsh_history" in service["volumes"]

def test_extract_meta_missing_key():
    yaml_dict = {"services": {}}
    compiler = YAMLCompiler()
    meta = compiler.extract_meta(yaml_dict)
    assert meta == {}

def test_graft_caddy_loopbacks_missing_labels():
    compiler = YAMLCompiler()
    service = {}
    ip = "127.0.1.5"
    compiler.graft_caddy_loopback(service, ip)
    assert f"caddy.listen={ip}:443" in service["labels"]

def test_format_host_volume_mappings_missing_volumes():
    compiler = YAMLCompiler()
    project_dir = "/home/user/myproject"
    service = {}
    compiler.format_telemetry_volumes(service, project_dir)
    assert f"{project_dir}/.sandbox/logs/admin/.bash_history:/home/dev/.bash_history" in service["volumes"]
    assert f"{project_dir}/.sandbox/logs/admin/.zsh_history:/home/dev/.zsh_history" in service["volumes"]
