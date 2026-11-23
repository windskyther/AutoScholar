"""Operator-only maintenance, cold snapshots and fresh-project restore via Docker."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from autoscholar.deployment.archives import inventory, sha256

WRITERS = ["web", "api", "workflow-worker", "document-worker", "sandbox-manager"]
ARCHIVES = ("documents", "workspaces", "redis", "qdrant")
DATABASE_STATUS = """
SELECT json_build_object(
 'schema', (SELECT version_num FROM alembic_version),
 'postgres_major', current_setting('server_version_num')::int / 10000,
 'projects', (SELECT count(*) FROM projects),
 'tasks', (SELECT count(*) FROM agent_tasks),
 'events', (SELECT count(*) FROM workflow_events),
 'artifacts', (SELECT count(*) FROM artifacts),
 'pending_approvals', (SELECT count(*) FROM workflow_approvals WHERE status='pending'),
 'pending_calls', (SELECT count(*) FROM workflow_jobs
                   WHERE pending_calls::text NOT IN ('{}','null')),
 'unresolved_invocations', (SELECT count(*) FROM core_tool_calls WHERE status != 'completed'),
 'expired_leases', (SELECT count(*) FROM workflow_jobs WHERE lease_until < now()),
 'usage_totals', (SELECT coalesce(json_object_agg(key,total),'{}') FROM
   (SELECT entry.key,sum(entry.value::text::bigint) AS total FROM workflow_jobs,
    LATERAL json_each(usage::json) AS entry GROUP BY entry.key) AS totals),
 'rate_limited', (SELECT count(*) FROM security_audit WHERE status=429),
 'request_errors', (SELECT count(*) FROM security_audit WHERE status>=500),
 'latency_p95_ms', (SELECT percentile_cont(0.95) WITHIN GROUP
   (ORDER BY extract(epoch FROM (finished-started))*1000) FROM
   (SELECT request_id,min(created_at) AS started,max(created_at) AS finished
    FROM security_audit WHERE phase IN ('admitted','completed')
    AND created_at > now()-interval '1 hour' GROUP BY request_id
    HAVING count(DISTINCT phase)=2) AS durations),
 'jobs', (SELECT coalesce(json_object_agg(status,n),'{}') FROM
   (SELECT status,count(*) AS n FROM workflow_jobs GROUP BY status) AS grouped))
"""


def verify_backup(folder: Path) -> dict[str, Any]:
    if (folder / "manifest.json").stat().st_size > 256_000:
        raise ValueError("Backup manifest exceeds limit")
    manifest: dict[str, Any] = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or set(manifest["files"]) != {
        "postgres.dump",
        *(n + ".tar" for n in ARCHIVES),
    }:
        raise ValueError("Incomplete or unsupported backup manifest")
    for name, expected in manifest["files"].items():
        path = folder / name
        if (
            path.is_symlink()
            or not path.is_file()
            or not re.fullmatch(r"[a-f0-9]{64}", expected["sha256"])
            or path.stat().st_size != expected["bytes"]
            or sha256(path) != expected["sha256"]
        ):
            raise ValueError("Backup checksum mismatch")
        if name.endswith(".tar"):
            inventory(path)
    return manifest


def private_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    if sys.platform != "win32":
        folder.chmod(0o700)
        return
    # Set ACL only on this newly created backup directory, never an existing tree.
    code = """
    $ErrorActionPreference='Stop'
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
    $target=$env:AUTOSCHOLAR_BACKUP_ACL_TARGET
    $acl=[IO.Directory]::GetAccessControl($target,[Security.AccessControl.AccessControlSections]::Access)
    $acl.SetAccessRuleProtection($true,$false)
    foreach($identity in @($sid.Value,'S-1-5-18','S-1-5-32-544')) {
      $principal=[Security.Principal.SecurityIdentifier]::new($identity)
      $rule=[Security.AccessControl.FileSystemAccessRule]::new($principal,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
      $acl.AddAccessRule($rule)
    }
    [IO.Directory]::SetAccessControl($target,$acl)
    """
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        env={**os.environ, "AUTOSCHOLAR_BACKUP_ACL_TARGET": str(folder)},
        check=True,
        capture_output=True,
    )


class Operations:
    def __init__(
        self, root: Path, project: str, files: list[str], env_file: str, *, image: str | None = None
    ) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
            raise ValueError("Invalid explicit project name")
        self.root, self.project = root, project
        self.env = {
            **os.environ,
            "COMPOSE_PROJECT_NAME": project,
            "AUTOSCHOLAR_LOCK_SHA256": sha256(root / "uv.lock"),
        }
        if image is not None:
            self.env["AUTOSCHOLAR_API_IMAGE"] = image
        self.command = ["docker", "compose", "--env-file", env_file, "-p", project]
        for file in files:
            self.command += ["-f", file]
        self.config = json.loads(self.run(*self.command, "config", "--format", "json"))
        for value in self.config["volumes"].values():
            if value.get("external") or not value["name"].startswith(project + "_"):
                raise ValueError("Operations require private project-owned volumes")
        self.image = self.config["services"]["api"]["image"]

    def run(self, *args: str) -> str:
        process = subprocess.run(
            args,
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if process.returncode:
            raise RuntimeError("Docker maintenance command failed; inspect service health locally")
        return process.stdout.strip()

    def compose(self, *args: str) -> str:
        return self.run(*self.command, *args)

    def record(self, action: str, outcome: str) -> None:
        directory = self.root / "data/deployment/operations" / self.project
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not directory.resolve().is_relative_to(self.root / "data"):
            raise ValueError("Unsafe maintenance audit directory")
        path = directory / "events.jsonl"
        if path.is_symlink():
            raise ValueError("Maintenance audit cannot be a link")
        with path.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps(
                    {
                        "time": datetime.now(UTC).isoformat(),
                        "project": self.project,
                        "actor": "local-docker-operator",
                        "action": action,
                        "outcome": outcome,
                    }
                )
                + "\n"
            )
            output.flush()
            os.fsync(output.fileno())

    def containers(self) -> list[dict[str, Any]]:
        ids = self.run(
            "docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.project
        ).split()
        values: list[dict[str, Any]] = (
            json.loads(self.run("docker", "inspect", *ids)) if ids else []
        )
        for record in values:
            if record["Config"]["Labels"]["com.docker.compose.project"] != self.project:
                raise ValueError("Maintenance ownership mismatch")
        return values

    def quiescent(self) -> None:
        for item in self.containers():
            if (
                item["Config"]["Labels"]["com.docker.compose.service"] in WRITERS
                and item["State"]["Running"]
            ):
                raise ValueError("Enter maintenance before backup, restore or upgrade")

    def database(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(
            self.compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "autoscholar",
                "-d",
                "autoscholar",
                "-Atc",
                DATABASE_STATUS,
            )
        )
        return value

    def status(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "database": self.database(),
            "services": [
                {
                    "service": item["Config"]["Labels"]["com.docker.compose.service"],
                    "image": item["Image"],
                    "state": item["State"]["Status"],
                    "health": item["State"].get("Health", {}).get("Status"),
                    "oom_killed": item["State"].get("OOMKilled", False),
                }
                for item in self.containers()
            ],
        }

    def enter(self) -> None:
        self.record("maintenance", "started")
        self.compose("stop", "--timeout", "45", *WRITERS)
        self.quiescent()
        self.record("maintenance", "completed")

    def helper(self, volume: str, folder: Path, role: str, action: str, checksum: str = "") -> str:
        destination = "/source" if action == "pack" else "/target"
        args = [
            "docker",
            "create",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "FOWNER",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "512m",
            "--pids-limit",
            "64",
            "--label",
            "autoscholar.maintenance=" + self.project,
            "--mount",
            f"type=volume,source={volume},target={destination}"
            + (",readonly" if action == "pack" else ""),
            "--mount",
            f"type=bind,source={folder},target=/backup"
            + (",readonly" if action == "unpack" else ""),
            "--mount",
            "type=bind,source="
            + str(self.root / "src/autoscholar/deployment/archives.py")
            + ",target=/archive_tool.py,readonly",
            "--entrypoint",
            "python",
            self.image,
            "-B",
            "/archive_tool.py",
            action,
            "/backup/" + role + ".tar",
            "--directory",
            destination,
        ]
        if checksum:
            args += ["--sha256", checksum]
        identifier = self.run(*args)
        if not re.fullmatch(r"[a-f0-9]{64}", identifier):
            raise ValueError("Maintenance helper identity missing")
        try:
            output = self.run("docker", "start", "--attach", identifier)
            record = json.loads(self.run("docker", "inspect", identifier))[0]
            if record["State"]["ExitCode"] != 0:
                raise RuntimeError("Backup/restore helper failed")
            return output
        finally:
            record = json.loads(self.run("docker", "inspect", identifier))[0]
            if record["Config"]["Labels"].get("autoscholar.maintenance") != self.project:
                raise ValueError("Maintenance helper ownership changed")
            self.run("docker", "rm", "--force", identifier)

    def backup(self, folder: Path) -> dict[str, Any]:
        self.quiescent()
        self.record("backup", "started")
        private_folder(folder)
        database = self.database()
        dump = folder / "postgres.dump"
        with dump.open("xb") as output:
            subprocess.run(
                [
                    *self.command,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "autoscholar",
                    "-d",
                    "autoscholar",
                    "-Fc",
                    "--no-owner",
                    "--no-acl",
                ],
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=subprocess.PIPE,
                check=True,
                timeout=300,
            )
        dump.chmod(0o600)
        # Redis clean shutdown preserves AOF, absolute expirations, limits and leases.
        # Qdrant's stopped-volume snapshot includes its complete WAL/storage state.
        self.compose("stop", "--timeout", "45", "redis", "qdrant")
        try:
            for item in self.containers():
                if item["Config"]["Labels"]["com.docker.compose.service"] in {
                    "redis",
                    "qdrant",
                } and (item["State"]["Running"] or item["State"]["ExitCode"] != 0):
                    raise ValueError("Storage did not stop cleanly; backup refused")
            files: dict[str, Any] = {
                "postgres.dump": {"sha256": sha256(dump), "bytes": dump.stat().st_size}
            }
            for role in ARCHIVES:
                volume = self.config["volumes"][role]["name"]
                info = json.loads(self.run("docker", "volume", "inspect", volume))[0]
                if info.get("Labels", {}).get("com.docker.compose.project") != self.project:
                    raise ValueError("Backup volume ownership mismatch")
                files[role + ".tar"] = json.loads(self.helper(volume, folder, role, "pack"))
            manifest = {
                "format": 1,
                "created": datetime.now(UTC).isoformat(),
                "project": self.project,
                "database": database,
                "api_image": self.image,
                "files": files,
                "services": self.status()["services"],
                "consumers_stopped": True,
                "qdrant_snapshot": "consistent cold volume",
                "redis_state": "complete AOF/RDB; no reset",
            }
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            verify_backup(folder)
            self.record("backup", "completed")
            return manifest
        finally:
            self.compose("up", "-d", "--no-build", "redis", "qdrant")

    def restore(self, folder: Path) -> dict[str, Any]:
        manifest = verify_backup(folder)
        if self.containers():
            raise ValueError(
                "Restore requires a fresh project; existing services are never overwritten"
            )
        existing = set(self.run("docker", "volume", "ls", "-q").split())
        if any(v["name"] in existing for v in self.config["volumes"].values()):
            raise ValueError("Restore target already has volumes")
        self.record("restore", "started")
        for role in ARCHIVES:
            volume = self.config["volumes"][role]["name"]
            self.run(
                "docker",
                "volume",
                "create",
                "--label",
                "com.docker.compose.project=" + self.project,
                "--label",
                "com.docker.compose.volume=" + role,
                volume,
            )
            self.helper(volume, folder, role, "unpack", manifest["files"][role + ".tar"]["sha256"])
        self.compose("up", "-d", "--no-build", "--wait", "postgres", "redis", "qdrant")
        with (folder / "postgres.dump").open("rb") as source:
            subprocess.run(
                [
                    *self.command,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_restore",
                    "-U",
                    "autoscholar",
                    "-d",
                    "autoscholar",
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                ],
                cwd=self.root,
                env=self.env,
                stdin=source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                timeout=300,
            )
        actual = self.database()
        for key in (
            "schema",
            "postgres_major",
            "projects",
            "tasks",
            "events",
            "artifacts",
            "pending_approvals",
            "pending_calls",
            "unresolved_invocations",
            "jobs",
        ):
            if actual[key] != manifest["database"][key]:
                raise ValueError("Restored database metadata differs")
        self.quiescent()
        self.record("restore", "completed_consumers_stopped")
        return {"restored": True, "database": actual, "consumers_started": False}

    def switch(self, *, resume: bool = False, backup: Path | None = None) -> dict[str, Any]:
        self.quiescent()
        database = self.database()
        metadata = json.loads(
            self.run(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "cat",
                self.image,
                "/app/build-info.json",
            )
        )
        if metadata["schema_revision"] != database["schema"]:
            raise ValueError(
                "Schema-incompatible switch refused; restore a verified backup to a new project"
            )
        if database["pending_calls"] or database["unresolved_invocations"]:
            raise ValueError("Unknown operations require reconciliation; workers remain stopped")
        if resume:
            target = json.loads(self.run("docker", "image", "inspect", self.image))[0]["Id"]
            current = [
                item["Image"]
                for item in self.containers()
                if item["Config"]["Labels"]["com.docker.compose.service"] == "api"
            ]
            if not current or any(value != target for value in current):
                if backup is None:
                    raise ValueError("An image switch requires a verified backup")
                previous = verify_backup(backup)
                for key in ("schema", "projects", "tasks", "events", "artifacts"):
                    if previous["database"][key] != database[key]:
                        raise ValueError("Backup does not match the maintenance database")
            self.record("resume", "started")
            self.compose("up", "-d", "--no-build", "--wait", *WRITERS)
            self.record("resume", "completed")
        return {"compatible": True, "image_revision": metadata["revision"], "resumed": resume}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["status", "maintenance", "backup", "restore", "check-switch", "resume"]
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--compose", action="append")
    parser.add_argument("--env-file", default=".env.production.example")
    parser.add_argument("--image")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    operation = Operations(
        root,
        args.project,
        args.compose or ["compose.production.yaml"],
        args.env_file,
        image=args.image,
    )
    if args.action == "status":
        result = operation.status()
    elif args.action == "maintenance":
        operation.enter()
        result = {"maintenance": True}
    elif args.action == "backup":
        folder = args.backup or root / "data/backups" / (args.project + "-" + uuid4().hex[:12])
        if not folder.resolve().is_relative_to(root / "data") or folder.is_symlink():
            raise ValueError("Backups must use a new private directory under data/")
        operation.backup(folder.resolve())
        result = {"backup": str(folder), "verified": True}
    elif args.action == "restore":
        if not args.backup:
            raise ValueError("Select a verified backup")
        result = operation.restore(args.backup.resolve())
    else:
        result = operation.switch(resume=args.action == "resume", backup=args.backup)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
