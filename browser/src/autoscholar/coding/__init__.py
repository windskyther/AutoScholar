"""Coding-agent workspaces, tools, and sandbox integration."""

from autoscholar.coding.agent import CodingAgent, CodingLimits, CodingResult, CodingRunError
from autoscholar.coding.sandbox import SandboxClient, SandboxExecutor
from autoscholar.coding.workspace import (
    WorkspaceError,
    WorkspaceFile,
    WorkspaceManager,
)

__all__ = [
    "CodingAgent",
    "CodingLimits",
    "CodingResult",
    "CodingRunError",
    "SandboxClient",
    "SandboxExecutor",
    "WorkspaceError",
    "WorkspaceFile",
    "WorkspaceManager",
]
