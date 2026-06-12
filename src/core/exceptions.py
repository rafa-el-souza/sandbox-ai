# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
class SandboxExecutionError(Exception):
    """
    Domain exception for sandbox orchestration execution failures.
    Strictly masks physical host variables or complex python stack traces
    when communicating errors to the CLI/UI.
    """

    pass
