## Purpose

This specification defines the Squid proxy's explicit DNS nameserver configuration for gVisor network sentry compatibility. Under gVisor, Docker's internal DNS proxy (`127.0.0.11`) is unreachable from the sentry's loopback netstack — UDP packets to `127.0.0.11:53` time out. Squid requires an explicit `dns_nameservers` directive pointing at CoreDNS on `egress_net`.

## Requirements

### Requirement: Squid Explicit DNS Nameserver
The `squid.conf` template SHALL include a `dns_nameservers` directive pointing at the CoreDNS instance on `egress_net`. This directive SHALL use the `{{ coredns_egress_ip }}` Jinja2 variable, which is already emitted by `derive_static_ips()` and available in the hydration context.

#### Scenario: dns_nameservers directive present
- **WHEN** the source `templates/config/proxy/squid.conf` template is inspected
- **THEN** it contains `dns_nameservers {{ coredns_egress_ip }}`

#### Scenario: dns_nameservers uses Jinja2 variable
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** `dns_nameservers` is set to the CoreDNS `egress_net` IP address (not a hardcoded value, not `127.0.0.11`)

#### Scenario: dns_nameservers placed after http_port
- **WHEN** the source `templates/config/proxy/squid.conf` template is inspected
- **THEN** the `dns_nameservers` directive appears after the `http_port` line and before the ACL section

#### Scenario: No fallback to Docker internal DNS
- **WHEN** the rendered `squid.conf` is inspected
- **THEN** it does NOT contain `127.0.0.11` as a DNS nameserver
