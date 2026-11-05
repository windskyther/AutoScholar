import io
import tarfile
from pathlib import Path

import pytest

from autoscholar.deployment.archives import inventory, pack, sha256, unpack


def test_backup_roundtrip_and_tamper_rejection(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "project").mkdir()
    (source / "project/report.md").write_text("公开验收", encoding="utf-8")
    archive = tmp_path / "backup.tar"
    record = pack(source, archive)
    target = tmp_path / "restored"
    target.mkdir()
    unpack(archive, target, str(record["sha256"]))
    assert (target / "project/report.md").read_bytes() == (
        source / "project/report.md"
    ).read_bytes()
    with pytest.raises(ValueError, match="empty"):
        unpack(archive, target, str(record["sha256"]))
    empty = tmp_path / "empty"
    empty.mkdir()
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        unpack(archive, empty, str(record["sha256"]))
    assert not list(empty.iterdir())


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("a\\..\\escape", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hard", tarfile.LNKTYPE),
        ("device", tarfile.CHRTYPE),
    ],
)
def test_unsafe_archives_fail_before_writing(tmp_path: Path, name: str, kind: bytes) -> None:
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as bundle:
        member = tarfile.TarInfo(name)
        member.type = kind
        bundle.addfile(member, io.BytesIO(b""))
    destination = tmp_path / "restore"
    destination.mkdir()
    with pytest.raises(ValueError):
        unpack(archive, destination, sha256(archive))
    assert not list(destination.iterdir())


def test_ancestor_collision_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "collision.tar"
    with tarfile.open(archive, "w") as bundle:
        for name in ("parent", "parent/child"):
            bundle.addfile(tarfile.TarInfo(name), io.BytesIO(b""))
    with pytest.raises(ValueError, match="ancestor"):
        inventory(archive)
