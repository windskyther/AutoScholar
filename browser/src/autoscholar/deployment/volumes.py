"""Initialize only brand-new app volumes; existing unmarked data is never changed."""

import os
import sys
from pathlib import Path


def initialize(path: Path, *, uid: int = 10001, gid: int = 10001) -> None:
    if sys.platform == "win32":
        raise ValueError("Volume ownership initialization runs only inside Linux containers")
    marker = path / ".autoscholar-volume-v1"
    if path.is_symlink() or marker.is_symlink():
        raise ValueError("Volume roots and ownership markers cannot be links")
    if marker.is_file():
        info = path.stat()
        if marker.read_text(encoding="ascii") != f"{uid}:{gid}\n" or (info.st_uid, info.st_gid) != (
            uid,
            gid,
        ):
            raise ValueError("Existing volume ownership requires a verified copy migration")
        return
    if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
        raise ValueError("Refuse to initialize a nonempty or indirect volume")
    # No recursion: legacy data must be migrated into a distinct copied volume.
    os.chown(path, uid, gid)
    marker.write_text(f"{uid}:{gid}\n", encoding="ascii")
    os.chown(marker, uid, gid)
    marker.chmod(0o600)


def main() -> None:
    for name in ("/data/documents", "/data/workspaces", "/models"):
        initialize(Path(name))


if __name__ == "__main__":
    main()
