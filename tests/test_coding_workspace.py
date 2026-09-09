import json
from pathlib import Path

import pytest

from autoscholar.coding.tools import WorkspaceToolset
from autoscholar.coding.workspace import WorkspaceError, WorkspaceManager


def test_workspace_creates_layout_and_manages_utf8_files(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    root = manager.initialize("task-1")

    assert {item.name for item in root.iterdir()} == set(manager.directories)
    created = manager.write_text("task-1", "模型/train.py", "print('训练')\n")
    assert created.path == "source/模型/train.py"
    assert manager.read_text("task-1", "模型/train.py") == "print('训练')\n"

    edited = manager.edit_text("task-1", "模型/train.py", "训练", "完成")
    assert edited.sha256 != created.sha256
    assert manager.search("task-1", "完成")[0]["line"] == 1

    manager.delete_file("task-1", "模型/train.py")
    assert manager.list_source_files("task-1") == []


@pytest.mark.parametrize(
    "unsafe_path",
    ["../.env", "source/../../.env", "/etc/passwd", "C:\\Windows\\system.ini", "./x.py"],
)
def test_workspace_rejects_escaping_or_ambiguous_paths(
    tmp_path: Path, unsafe_path: str
) -> None:
    manager = WorkspaceManager(tmp_path)
    manager.initialize("task-1")

    with pytest.raises(WorkspaceError, match="relative file path"):
        manager.write_text("task-1", unsafe_path, "unsafe")


def test_workspace_rejects_symlink_escape(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    root = manager.initialize("task-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "source" / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable for this account")

    with pytest.raises(WorkspaceError, match="Symbolic links"):
        manager.write_text("task-1", "link/escape.py", "unsafe")


def test_workspace_enforces_file_and_total_quotas(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path, max_files=1, max_file_bytes=5, max_source_bytes=5)
    manager.initialize("task-1")
    manager.write_text("task-1", "one.py", "12345")

    with pytest.raises(WorkspaceError) as too_large:
        manager.write_text("task-1", "large.py", "123456")
    assert too_large.value.code == "workspace_file_too_large"

    with pytest.raises(WorkspaceError) as too_many:
        manager.write_text("task-1", "two.py", "1")
    assert too_many.value.code == "workspace_file_limit"


def test_workspace_edit_requires_an_exact_single_match(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    manager.initialize("task-1")
    manager.write_text("task-1", "main.py", "same\nsame\n")

    with pytest.raises(WorkspaceError) as conflict:
        manager.edit_text("task-1", "main.py", "same", "new")
    assert conflict.value.code == "workspace_edit_conflict"
    assert manager.read_text("task-1", "main.py") == "same\nsame\n"


async def test_workspace_tools_are_scoped_and_return_structured_results(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    manager.initialize("task-1")
    tools = {tool.definition.name: tool for tool in WorkspaceToolset(manager, "task-1").tools()}

    created = await tools["create_file"].execute({"path": "main.py", "content": "print(1)\n"})
    listed = await tools["list_files"].execute({})
    escaped = await tools["read_file"].execute({"path": "../../.env"})

    assert created.succeeded is True
    assert json.loads(listed.output)[0]["path"] == "source/main.py"
    assert escaped.succeeded is False
    assert escaped.error_code == "workspace_path_invalid"
