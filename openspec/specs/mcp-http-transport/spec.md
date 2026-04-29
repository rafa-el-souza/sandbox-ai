## Purpose

This specification defines the Streamable HTTP transport for the firecrawl MCP container, replacing the legacy socat+UDS bridge with `mcp-proxy`, and the programmatic generation of `.claude.json` for MCP server registration.

## Requirements

### Requirement: Firecrawl mcp-proxy Entrypoint
The firecrawl container SHALL use `mcp-proxy` as its entrypoint, serving Streamable HTTP on port 3000. The legacy socat UDS bridge SHALL be removed.

#### Scenario: mcp-proxy entrypoint replaces socat
- **WHEN** the rendered `mcp-firecrawl.yml` is inspected
- **THEN** the entrypoint is `["npx", "-y", "mcp-proxy", "--port", "3000", "--server", "stream", "--", "npx", "-y", "firecrawl-mcp"]`

#### Scenario: socat removed from firecrawl image
- **WHEN** the firecrawl Dockerfile is inspected
- **THEN** it does NOT install `socat`

#### Scenario: mcp-proxy serves only Streamable HTTP
- **WHEN** `mcp-proxy` starts with `--server stream`
- **THEN** it exposes only the `/mcp` endpoint (Streamable HTTP); the deprecated SSE endpoint is disabled

### Requirement: Programmatic .claude.json Generation
The system SHALL generate `.claude.json` programmatically via `json.dump` in `render_templates()`, with conditional `mcpServers` registration based on enabled MCP components. `.claude.json` SHALL be removed from `_STATIC_CONFIG_CORE`.

#### Scenario: .claude.json generated with firecrawl MCP when enabled
- **WHEN** `render_templates()` runs with `mcp_firecrawl_enabled = True`
- **THEN** the generated `.claude.json` contains `{"mcpServers": {"firecrawl": {"type": "http", "url": "http://<firecrawl_isolated_ip>:3000/mcp"}}}`

#### Scenario: .claude.json generated empty when no MCP enabled
- **WHEN** `render_templates()` runs with `mcp_firecrawl_enabled = False`
- **THEN** the generated `.claude.json` contains `{}`

#### Scenario: .claude.json removed from static config list
- **WHEN** `_STATIC_CONFIG_CORE` is inspected
- **THEN** it does NOT contain `.claude.json`

#### Scenario: .claude.json uses json.dump not Jinja2
- **WHEN** the `.claude.json` generation code is inspected
- **THEN** it uses `json.dump` with `indent=2` (not Jinja2 template rendering)

### Requirement: mcp-ipc_vol Removal
The `mcp-ipc_vol` Docker named volume SHALL be removed from the compose templates. Neither core nor firecrawl SHALL mount `/var/run/mcp`.

#### Scenario: mcp-ipc_vol absent from compose
- **WHEN** the rendered `compose.yml` and `mcp-firecrawl.yml` are inspected
- **THEN** neither contains a volume definition for `mcp-ipc_vol`

#### Scenario: /var/run/mcp mount absent
- **WHEN** the rendered `compose.yml` (core) and `mcp-firecrawl.yml` are inspected
- **THEN** neither contains a volume mount referencing `/var/run/mcp`
