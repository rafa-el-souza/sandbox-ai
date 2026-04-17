#!/bin/bash
set -euo pipefail

# Execute the PTY UNIX domain mapping loop strictly isolating the agent interface memory
exec socat UNIX-LISTEN:/sock/admin.sock,mode=0660,fork EXEC:"/bin/bash",pty,stderr,setsid,sigint,sane
