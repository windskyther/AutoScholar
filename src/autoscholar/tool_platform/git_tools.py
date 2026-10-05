"""Three bounded Git contracts, scoped to the coding task rather than the developer checkout."""

from autoscholar.llm import ToolDefinition
from autoscholar.tool_platform.filesystem import FILE_OUTPUT, MCPFileTool
from autoscholar.tool_platform.gateway import ToolContract, ToolGateway

GIT_CONTRACTS = [
    ToolContract(
        "clone_repo",
        {
            "type": "object",
            "properties": {"repo_url": {"type": "string", "minLength": 1, "maxLength": 512}},
            "required": ["repo_url"],
            "additionalProperties": False,
        },
        FILE_OUTPUT,
    ),
    *[
        ToolContract(
            name,
            {
                "type": "object",
                "properties": {"repo_id": {"type": "string", "pattern": "^repo-[0-9a-f]{16}$"}},
                "required": ["repo_id"],
                "additionalProperties": False,
            },
            FILE_OUTPUT,
        )
        for name in ("git_status", "git_diff")
    ],
]


def git_tools(gateway: ToolGateway, task_id: str) -> list[MCPFileTool]:
    descriptions = {
        "clone_repo": "Import an allowlisted public HTTPS text repository into task sources.",
        "git_status": "Read task repository status using the repo_id returned by clone_repo.",
        "git_diff": "Read a bounded diff of task source edits against the imported snapshot.",
    }
    return [
        MCPFileTool(
            ToolDefinition(
                name=c.name, description=descriptions[c.name], parameters=c.input_schema
            ),
            gateway,
            task_id,
        )
        for c in GIT_CONTRACTS
    ]
