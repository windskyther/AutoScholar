import hashlib
import json
import os
from pathlib import Path

from torchvision.datasets import MNIST

root = Path(os.getenv("MNIST_ROOT", "/datasets/mnist"))
MNIST(root=root, train=True, download=True)
MNIST(root=root, train=False, download=True)
for path in root.rglob("*"):
    path.chmod(0o755 if path.is_dir() else 0o644)

digest = hashlib.sha256()
for path in sorted(item for item in root.rglob("*") if item.is_file()):
    if path.name == ".autoscholar-ready":
        continue
    digest.update(path.relative_to(root).as_posix().encode("utf-8"))
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1_048_576), b""):
            digest.update(block)

manifest = {
    "dataset_id": "mnist",
    "dataset_sha256": digest.hexdigest(),
    "train_samples": 60_000,
    "test_samples": 10_000,
}
(root / ".autoscholar-ready").write_text(
    json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
)
