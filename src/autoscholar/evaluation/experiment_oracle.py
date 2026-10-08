# mypy: ignore-errors
"""Sandbox-only independent checkpoint/data/plot verification, never imported on the host."""

import base64
import hashlib
import json
import math
import os
from pathlib import Path

import torch
from checkpoint_models import CNN, MLP
from PIL import Image
from torch.utils.data import DataLoader, Subset
from torchvision.datasets import MNIST
from torchvision.transforms import ToTensor


def main():
    torch.set_num_threads(2)
    payload = json.loads(Path("oracle_payload.json").read_text(encoding="utf-8"))
    spec = payload["specification"]
    raw = payload["raw_metrics"]
    dataset_root = Path(os.environ["MNIST_ROOT"])
    manifest = json.loads((dataset_root / ".autoscholar-ready").read_text(encoding="utf-8"))
    digest = hashlib.sha256()
    for path in sorted(item for item in dataset_root.rglob("*") if item.is_file()):
        if path.name == ".autoscholar-ready":
            continue
        digest.update(path.relative_to(dataset_root).as_posix().encode())
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1048576), b""):
                digest.update(block)
    dataset_verified = (
        manifest["dataset_id"] == "mnist"
        and digest.hexdigest() == manifest["dataset_sha256"] == payload["dataset_sha256"]
    )
    dataset = MNIST(root=str(dataset_root), train=False, download=False, transform=ToTensor())
    indices = torch.randperm(
        len(dataset), generator=torch.Generator().manual_seed(spec["seed"] + 1)
    )[: spec["test_samples"]].tolist()
    loader = DataLoader(Subset(dataset, indices), batch_size=spec["batch_size"], shuffle=False)
    results = []
    for index, constructor in enumerate((MLP, CNN)):
        encoded = "".join(
            Path(path).read_text(encoding="ascii") for path in payload["checkpoints"][index]
        )
        checkpoint = Path(f"checkpoint-{index}.pt")
        checkpoint.write_bytes(base64.b64decode(encoded, validate=True))
        # Never unpickle unrestricted checkpoint objects, and never load them on the host.
        state = torch.load(checkpoint, map_location="cpu", weights_only=True)
        model = constructor()
        model.load_state_dict(state, strict=True)
        finite = all(torch.isfinite(tensor).all().item() for tensor in state.values())
        model.eval()
        correct = 0
        with torch.no_grad():
            for images, targets in loader:
                correct += (model(images).argmax(dim=1) == targets).sum().item()
        accuracy = correct / len(indices)
        parameters = sum(parameter.numel() for parameter in model.parameters())
        measured = raw["runs"][index]
        results.append(
            {
                "model": ("mlp", "cnn")[index],
                "accuracy": accuracy,
                "parameters": parameters,
                "verified": finite
                and parameters == measured["parameters"]
                and math.isclose(accuracy, measured["test_accuracy"], rel_tol=0, abs_tol=1e-9),
            }
        )
    plots_verified = True
    for path in payload["plots"]:
        data = base64.b64decode(Path(path).read_text(encoding="ascii"), validate=True)
        destination = Path(path).with_suffix(".png")
        destination.write_bytes(data)
        with Image.open(destination) as image:
            plots_verified = plots_verified and image.format == "PNG" and image.size == (720, 420)
            image.verify()
    Path("outputs").mkdir(exist_ok=True)
    Path("outputs/eval_oracle.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_verified": dataset_verified,
                "plots_verified": plots_verified,
                "models": results,
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
