import os
from pathlib import Path

from torchvision.datasets import MNIST

root = Path(os.getenv("MNIST_ROOT", "/datasets/mnist"))
MNIST(root=root, train=True, download=True)
MNIST(root=root, train=False, download=True)
for path in root.rglob("*"):
    path.chmod(0o755 if path.is_dir() else 0o644)
(root / ".autoscholar-ready").write_text("MNIST ready\n", encoding="utf-8")
