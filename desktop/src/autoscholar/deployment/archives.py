"""Cold-volume archives: regular files only, bounded verification before restore."""

import argparse
import hashlib
import json
import os
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import IO

MAX_FILES = 200_000
MAX_BYTES = 32 * 1024**3


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts or ":" in name:
        raise ValueError("Unsafe backup member path")
    return path


def inventory(archive: Path) -> list[tarfile.TarInfo]:
    members: list[tarfile.TarInfo] = []
    names: set[str] = set()
    total = 0
    with tarfile.open(archive, "r:") as bundle:
        for item in bundle:
            path = member_path(item.name)
            if str(path) == "." and not item.isdir():
                raise ValueError("Archive root must be a directory")
            if str(path) in names or not (item.isdir() or item.isfile()):
                raise ValueError("Duplicate or non-regular backup member")
            if item.uid < 0 or item.gid < 0 or item.uid > 65535 or item.gid > 65535:
                raise ValueError("Invalid backup ownership")
            names.add(str(path))
            total += item.size
            if item.size < 0 or total > MAX_BYTES or len(members) >= MAX_FILES:
                raise ValueError("Backup exceeds restore limits")
            members.append(item)
    # A file cannot be used as the ancestor of another entry.
    files = {str(member_path(item.name)) for item in members if item.isfile()}
    if any(str(parent) in files for item in members for parent in member_path(item.name).parents):
        raise ValueError("Backup member has a file ancestor")
    return members


def pack(source: Path, archive: Path) -> dict[str, int | str]:
    if source.is_symlink() or not source.is_dir() or archive.exists():
        raise ValueError("Backup requires a real source and a new archive")
    count = size = 0
    try:
        with archive.open("xb") as output, tarfile.open(fileobj=output, mode="w") as bundle:
            root = bundle.gettarinfo(str(source), arcname=".")
            root.mode &= 0o777
            bundle.addfile(root)
            count = 1
            for folder, directories, files in os.walk(source, followlinks=False):
                for name in sorted(directories + files):
                    path = Path(folder) / name
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode) or not (
                        stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                    ):
                        raise ValueError("Backup source contains a link or special file")
                    item = bundle.gettarinfo(str(path), arcname=path.relative_to(source).as_posix())
                    # Materialize hardlinked files independently; restore accepts no links.
                    item.mode &= 0o777
                    if path.is_file():
                        item.type = tarfile.REGTYPE
                        item.linkname = ""
                        item.size = info.st_size
                        size += item.size
                        with path.open("rb") as content:
                            bundle.addfile(item, content)
                    else:
                        bundle.addfile(item)
                    count += 1
                    if count > MAX_FILES or size > MAX_BYTES:
                        raise ValueError("Backup exceeds restore limits")
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    inventory(archive)
    return {"sha256": sha256(archive), "bytes": archive.stat().st_size, "members": count}


def copy(source: IO[bytes], output: IO[bytes]) -> None:
    for block in iter(lambda: source.read(1024 * 1024), b""):
        output.write(block)


def unpack(archive: Path, target: Path, expected: str) -> None:
    if sha256(archive) != expected:
        raise ValueError("Backup checksum mismatch")
    members = inventory(archive)  # Validate every header before writing anything.
    if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
        raise ValueError("Restore requires an empty, real destination")
    with tarfile.open(archive, "r:") as bundle:
        for item in members:
            path = target.joinpath(*member_path(item.name).parts)
            if item.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                content = bundle.extractfile(item)
                if content is None:
                    raise ValueError("Missing backup content")
                with content, path.open("xb") as output:
                    copy(content, output)
        # Restore permissions after all children exist; never restore SUID/SGID bits.
        for item in reversed(members):
            path = target.joinpath(*member_path(item.name).parts)
            if sys.platform != "win32":
                os.chown(path, item.uid, item.gid)
            path.chmod(item.mode & 0o777)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["pack", "unpack"])
    parser.add_argument("archive", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    if args.action == "pack":
        print(json.dumps(pack(args.directory, args.archive)))
    else:
        if not args.sha256:
            raise ValueError("Restore requires an expected checksum")
        unpack(args.archive, args.directory, args.sha256)
        print(json.dumps({"restored": True}))


if __name__ == "__main__":
    main()
