## Purpose

This specification defines the Squid proxy ACL hardening controls, including IP-literal bypass prevention via defense-in-depth deny layers, per-container source binding, read-only registry method restriction, and request body size limits.

## Requirements

### Requirement: IP-Literal Proxy Bypass Prevention
The Squid proxy configuration SHALL deny HTTP requests targeting IP addresses via a three-layer defense-in-depth architecture. Layer 1 SHALL use `dst` ACLs to deny resolved IP addresses in RFC 1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), loopback (`127.0.0.0/8`), link-local/cloud-metadata (`169.254.0.0/16`), and CGN (`100.64.0.0/10`) ranges. Layer 2 SHALL use a `dstdom_regex` ACL to deny dotted-decimal IP patterns in URL hostnames. Layer 3 is the existing `dstdomain` allowlist as structural deny-by-default. All deny rules SHALL be placed before the allow rule in `squid.conf`.

#### Scenario: RFC 1918 IP-literal request denied
- **WHEN** the agent sends an HTTP request through the proxy targeting `http://192.168.1.1/secret`
- **THEN** the proxy denies the request via the `dst` ACL matching `192.168.0.0/16`

#### Scenario: Loopback IP-literal request denied
- **WHEN** the agent sends an HTTP request through the proxy targeting `http://127.0.0.1:8080/`
- **THEN** the proxy denies the request via the `dst` ACL matching `127.0.0.0/8`

#### Scenario: Cloud metadata IP-literal request denied
- **WHEN** the agent sends an HTTP request through the proxy targeting `http://169.254.169.254/latest/meta-data/`
- **THEN** the proxy denies the request via the `dst` ACL matching `169.254.0.0/16`

#### Scenario: CGN IP-literal request denied
- **WHEN** the agent sends an HTTP request through the proxy targeting `http://100.64.0.1/`
- **THEN** the proxy denies the request via the `dst` ACL matching `100.64.0.0/10`

#### Scenario: DNS rebinding to private IP denied
- **WHEN** an allowlisted domain resolves to `10.0.0.1` (DNS rebinding)
- **THEN** the proxy denies the request via the `dst` ACL matching `10.0.0.0/8` after DNS resolution

#### Scenario: Dotted-decimal hostname pattern denied at string level
- **WHEN** the agent sends an HTTP request with a dotted-decimal hostname (e.g., `http://1.2.3.4/path`)
- **THEN** the proxy denies the request via the `dstdom_regex` ACL matching `^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$`

#### Scenario: dstdom_regex does not false-positive on URL paths
- **WHEN** the agent sends an HTTP request to `https://api.example.com/v1/10.0.0.1/status` (IP in path, not hostname)
- **THEN** the proxy does NOT deny the request based on the IP-literal `dstdom_regex` (regex matches hostname only)

#### Scenario: dst ACLs do not use -n flag
- **WHEN** the `squid.conf` template is inspected
- **THEN** all `dst` ACLs for private ranges do NOT include the `-n` flag (DNS resolution is intentional for rebinding defense)

#### Scenario: Separate ACL names per CIDR range
- **WHEN** the `squid.conf` template is inspected
- **THEN** each CIDR range has its own named ACL (e.g., `deny_rfc1918_10`, `deny_rfc1918_172`, `deny_rfc1918_192`, `deny_loopback`, `deny_link_local`, `deny_cgn`) to avoid splay tree ordering issues

### Requirement: Per-Container Proxy Source Binding
The Squid proxy configuration SHALL bind proxy authentication to specific container IP addresses using `acl agent_src src {{ agent_proxy_ip }}` and `acl admin_src src {{ admin_proxy_ip }}`. The allow rule SHALL reference these per-IP ACLs instead of a subnet-wide ACL.

#### Scenario: Agent source ACL bound to specific IP
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** it contains `acl agent_src src {{ agent_proxy_ip }}` (resolved from `derive_static_ips()`)

#### Scenario: Admin source ACL bound to specific IP
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** it contains `acl admin_src src {{ admin_proxy_ip }}` (resolved from `derive_static_ips()`)

#### Scenario: Allow rules use per-IP ACLs
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** it contains `http_access allow agent_src authenticated_users whitelist` and `http_access allow admin_src authenticated_users whitelist`

#### Scenario: No subnet-wide trusted_clients ACL
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** it contains no `trusted_clients` ACL definition and no reference to `trusted_clients`

#### Scenario: trusted_clients.acl file not generated
- **WHEN** the hydration pipeline runs
- **THEN** no `trusted_clients.acl` file is generated in the instance config directory

#### Scenario: trusted_clients.acl not mounted in compose
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** it contains no volume mount for `trusted_clients.acl`

### Requirement: Read-Only Registry Method Restriction
The Squid proxy configuration SHALL deny `POST`, `PUT`, `PATCH`, and `DELETE` methods to domains listed in a `read_only_domains.txt` file. The deny rule SHALL be placed before the allow rule.

#### Scenario: POST to read-only registry denied
- **WHEN** the agent sends a `POST` request through the proxy to `pypi.org`
- **THEN** the proxy denies the request via the `write_methods` + `read_only_registries` ACL

#### Scenario: GET to read-only registry allowed
- **WHEN** the agent sends a `GET` request through the proxy to `pypi.org` with valid credentials and the domain is allowlisted
- **THEN** the proxy allows the request (method deny does not apply to GET/HEAD)

#### Scenario: Read-only domains file mounted in proxy
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the proxy service has a volume mount for `read_only_domains.txt` at `/etc/squid/read_only_domains.txt:ro`

#### Scenario: Read-only domains file generated from context
- **WHEN** the hydration pipeline runs with `proxy.whitelist.read_only_domains` configured
- **THEN** `config/proxy/read_only_domains.txt` is generated containing one domain per line

### Requirement: Request Body Size Limit
The Squid proxy configuration SHALL set `request_body_max_size` to `2 MB`.

#### Scenario: Body size set to 2 MB
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** it contains `request_body_max_size 2 MB`

### Requirement: IP-Literal Deny Rule Ordering
All IP-literal deny rules and method deny rules SHALL be placed before the allow rule in `squid.conf`, ensuring deny-before-allow semantics.

#### Scenario: Deny rules precede allow rules
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** all `http_access deny` rules for IP-literal ranges, `dstdom_regex`, and `write_methods` appear before any `http_access allow` rule

### Requirement: Firecrawl Per-Container Proxy Source Binding
The Squid proxy configuration SHALL bind firecrawl proxy access to its specific container IP address using `acl firecrawl_src src {{ mcp_firecrawl_proxy_ip }}`. The allow rule SHALL restrict firecrawl to safe HTTP methods (GET, HEAD, OPTIONS) only.

#### Scenario: Firecrawl source ACL bound to specific IP
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** it contains `acl firecrawl_src src {{ mcp_firecrawl_proxy_ip }}` (resolved from `derive_static_ips()`)

#### Scenario: Safe methods ACL defined
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** it contains `acl safe_methods method GET HEAD OPTIONS`

#### Scenario: Firecrawl allow rule restricted to safe methods
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** it contains `http_access allow firecrawl_src authenticated_users safe_methods whitelist`

#### Scenario: Firecrawl POST denied
- **WHEN** firecrawl sends a POST request through the proxy to an allowlisted domain with valid credentials
- **THEN** the proxy denies the request because POST is not in `safe_methods` — the request falls through to `http_access deny all`

#### Scenario: Firecrawl allow rule after agent/admin allows
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** the `firecrawl_src` allow rule appears after the `agent_src` and `admin_src` allow rules and before the `http_access deny all` catch-all

#### Scenario: Firecrawl subject to all deny layers
- **WHEN** firecrawl sends an HTTP request through the proxy
- **THEN** the request is evaluated against all deny rules (IP-literal, read-only registries, conn_limit, IPv6) before reaching the firecrawl allow rule
