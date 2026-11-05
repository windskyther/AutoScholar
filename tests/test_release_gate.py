import pytest

from scripts.release_images import validate_version, verified_checks


@pytest.mark.parametrize(
    "version", ["latest", "v1", "v1.0.0;echo injected", "../v1.0.0", "v1.0.0\n"]
)
def test_release_version_cannot_be_a_command_or_ambiguous_tag(version: str) -> None:
    with pytest.raises(ValueError):
        validate_version(version)


def test_release_requires_success_on_exact_main_revision() -> None:
    validate_version("v1.0.0-rc.1")
    revision = "a" * 40
    run = {
        "head_sha": revision,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    assert verified_checks({"workflow_runs": [run]}, revision)
    for change in (
        {"head_sha": "b" * 40},
        {"head_branch": "dev"},
        {"conclusion": "failure"},
        {"status": "in_progress"},
        {"event": "pull_request"},
    ):
        assert not verified_checks({"workflow_runs": [{**run, **change}]}, revision)
