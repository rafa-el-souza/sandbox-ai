<!-- SANDBOX_CORE_RULES -->
You operate in a strict rootless container. Network routing is severed; do NOT execute network debugging tools (ping, traceroute). You do not have capabilities to spin up containers or system daemons natively. If a database, cache, or persistent background service is required for your task, do not attempt to install or start it natively. Instead, explicitly signal the human administrator to provide it as an isolated compose service globally.

### System Boundaries
# Dinamically append system boundaries
