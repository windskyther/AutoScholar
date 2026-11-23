"""Fresh production-fixture backup/restore, compatible upgrade/rollback, unknown-call stop."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from configure_production import initialize

from autoscholar.deployment.operations import Operations, verify_backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="autoscholar-api:prod-local")
    parser.add_argument("--web", default="autoscholar-web:prod-local")
    parser.add_argument("--previous-api", default="autoscholar-api:prod-local")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    owner = "phase11g-" + uuid4().hex[:12]
    output = root / "data/validation" / owner
    output.mkdir(parents=True)
    command = [
        sys.executable,
        "scripts/verify_production.py",
        "--api",
        args.previous_api,
        "--web",
        args.web,
        "--keep-stack",
    ]
    with (output / "ingress.log").open("w", encoding="utf-8") as log:
        process = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    if process.returncode:
        raise RuntimeError("Recovery source fixture failed; inspect ingress.log")
    answer = json.loads((output / "ingress.log").read_text(encoding="utf-8").splitlines()[-1])
    source_receipt = Path(answer["receipt"])
    source_info = json.loads(source_receipt.read_text(encoding="utf-8"))
    files = ["compose.production.yaml", "tests/integration/phase11.production.yaml"]
    old_env = os.environ.copy()
    os.environ.update(
        PROD_SECRETS_DIR=str(source_receipt.parent / "secrets"), AUTOSCHOLAR_WEB_IMAGE=args.web
    )
    source = Operations(
        root, source_info["project"], files, ".env.production.example", image=args.previous_api
    )
    target: Operations | None = None
    unknown_target: Operations | None = None
    report: dict[str, object] = {"accepted": False, "supplier_api_calls": 0, "owner": owner}
    try:
        # Credential handling stays inside the isolated container; print counters only.
        state_script = (
            "import json,redis,httpx,hashlib; from autoscholar.core.config import Settings; "
            "s=Settings(_env_file=None); r=redis.Redis.from_url(s.redis_url); "
            "r.set('phase11g:retained',17,ex=1800) if __import__('sys').argv[1]=='seed' else None; "
            "c=httpx.Client(trust_env=False,timeout=10); "
            "v=c.get(s.qdrant_url+'/collections',"
            "headers={'api-key':s.qdrant_api_key.get_secret_value()}); v.raise_for_status(); "
            "collections=v.json()['result']['collections']; "
            "points={}; "
            'exec("for collection in collections:\\n'
            " response=c.post(s.qdrant_url+'/collections/'+collection['name']+'/points/scroll',"
            "headers={'api-key':s.qdrant_api_key.get_secret_value()},"
            "json={'limit':100,'with_payload':True,'with_vector':True}); "
            "response.raise_for_status(); result=response.json()['result']; "
            "assert result['next_page_offset'] is None; "
            "points[collection['name']]=result['points']\"); "
            "files={str(root.name)+'/'+str(path.relative_to(root)):"
            "hashlib.sha256(path.read_bytes()).hexdigest() "
            "for root in (s.document_storage_path,s.workspace_root) "
            "for path in root.rglob('*') if path.is_file()}; "
            "print(json.dumps({'redis_value':int(r.get('phase11g:retained')), "
            "'ttl':r.ttl('phase11g:retained'), "
            "'collections':collections,'points':points,'files':files}))"
        )
        source.enter()
        source.compose(
            "run", "--rm", "--no-deps", "api", "python", "/acceptance/fixture.py", "seed-pending"
        )
        before = json.loads(
            source.compose("run", "--rm", "--no-deps", "api", "python", "-c", state_script, "seed")
        )
        backup = output / "backup"
        manifest = source.backup(backup)
        assert manifest["database"]["pending_approvals"] > 0
        # Corrupt only this newly generated public fixture dump and require rejection.
        dump = backup / "postgres.dump"
        data = dump.read_bytes()
        dump.write_bytes(data + b"tamper")
        try:
            verify_backup(backup)
            raise AssertionError("Tampered backup was accepted")
        except ValueError:
            report["tamper_rejected"] = True
        finally:
            dump.write_bytes(data)
        verify_backup(backup)
        secrets = output / "target-secrets"
        initialize(secrets)
        os.environ["PROD_SECRETS_DIR"] = str(secrets)
        target = Operations(
            root, owner + "-restored", files, ".env.production.example", image=args.previous_api
        )
        restored = target.restore(backup)
        after = json.loads(
            target.compose("run", "--rm", "--no-deps", "api", "python", "-c", state_script, "check")
        )
        assert after["redis_value"] == before["redis_value"] == 17
        assert 0 < after["ttl"] <= before["ttl"] + 1
        assert after["collections"] == before["collections"]
        assert before["files"] and after["files"] == before["files"]
        assert any(before["points"].values()) and after["points"] == before["points"]
        report.update(
            restored=restored,
            redis_ttl_retained=True,
            qdrant_collections_retained=True,
            qdrant_points_payload_vectors_retained=True,
            document_workspace_file_hashes_retained=True,
            backup_manifest=str(backup / "manifest.json"),
        )
        target.image = args.api
        target.env["AUTOSCHOLAR_API_IMAGE"] = args.api
        upgraded = target.switch(resume=True, backup=backup)
        target.enter()
        target.image = args.previous_api
        target.env["AUTOSCHOLAR_API_IMAGE"] = args.previous_api
        rolled_back = target.switch(resume=True, backup=backup)
        target.enter()
        target.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "autoscholar",
            "-d",
            "autoscholar",
            "-c",
            'UPDATE workflow_jobs SET pending_calls=\'{"public-unknown":"unknown"}\' '
            "WHERE task_id=(SELECT task_id FROM workflow_jobs LIMIT 1)",
        )
        unknown_backup = output / "unknown-backup"
        target.backup(unknown_backup)
        unknown_secrets = output / "unknown-secrets"
        initialize(unknown_secrets)
        os.environ["PROD_SECRETS_DIR"] = str(unknown_secrets)
        unknown_target = Operations(
            root, owner + "-unknown", files, ".env.production.example", image=args.previous_api
        )
        unknown_target.restore(unknown_backup)
        assert unknown_target.database()["pending_calls"] == 1
        try:
            unknown_target.switch(resume=True, backup=unknown_backup)
            raise AssertionError("Unknown invocation did not stop resume")
        except ValueError as error:
            assert "Unknown operations" in str(error)
        unknown_target.quiescent()
        report.update(
            accepted=True,
            upgrade=upgraded,
            rollback=rolled_back,
            unknown_calls_block_resume=True,
            unknown_calls_preserved_through_restore=True,
            pending_approval_preserved=True,
            original_database=manifest["database"],
        )
    finally:
        cleanup_errors: list[str] = []
        for operation in (unknown_target, target, source):
            if operation is not None:
                try:
                    operation.compose("down", "--volumes", "--remove-orphans")
                    assert not operation.containers()
                except Exception as error:
                    cleanup_errors.append(type(error).__name__)
        os.environ.clear()
        os.environ.update(old_env)
        report["resources_removed"] = not cleanup_errors
        report["accepted"] = report["accepted"] and not cleanup_errors
        (output / "receipt.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"accepted": report["accepted"], "receipt": str(output / "receipt.json")}))
        if cleanup_errors:
            raise RuntimeError("Recovery fixture cleanup incomplete; inspect owned projects")


if __name__ == "__main__":
    main()
