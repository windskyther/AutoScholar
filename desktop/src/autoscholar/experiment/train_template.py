# mypy: ignore-errors
"""Trusted source template. The API reads this file as text; it runs only in the sandbox."""

import json
import os
import random
import time
from pathlib import Path

import torch
from PIL import Image, ImageDraw
from torch import nn
from torch.utils.data import DataLoader, Subset
from torchvision.datasets import MNIST
from torchvision.transforms import ToTensor


class MLP(nn.Module):
    def __init__(self):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Flatten(),
            nn.Linear(28 * 28, 128),
            nn.ReLU(),
            nn.Linear(128, 10),
        )

    def forward(self, images):
        return self.layers(images)


class CNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Conv2d(1, 8, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(8, 16, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Flatten(),
            nn.Linear(16 * 7 * 7, 10),
        )

    def forward(self, images):
        return self.layers(images)


def plot_series(runs, key, destination):
    canvas = Image.new("RGB", (720, 420), "white")
    draw = ImageDraw.Draw(canvas)
    left, top, right, bottom = 65, 35, 680, 350
    draw.line([(left, top), (left, bottom), (right, bottom)], fill="black", width=2)
    draw.text((left, 7), key.replace("_", " ").title(), fill="black")
    maximum = max(value for run in runs for value in run[key])
    ceiling = max(1.0, maximum * 1.1) if key == "train_loss" else 1.0
    colors = {"mlp": "#2876c7", "cnn": "#db6234"}
    for run in runs:
        values = run[key]
        points = []
        for index, value in enumerate(values):
            x = left + (right - left) * index / max(1, len(values) - 1)
            y = bottom - (bottom - top) * value / ceiling
            points.append((round(x), round(y)))
        if len(points) == 1:
            draw.ellipse((points[0][0] - 3, points[0][1] - 3,
                          points[0][0] + 3, points[0][1] + 3),
                         fill=colors[run["model"]])
        else:
            draw.line(points, fill=colors[run["model"]], width=3)
        draw.text((left + 150 * (0 if run["model"] == "mlp" else 1), 375),
                  run["model"].upper(), fill=colors[run["model"]])
    canvas.save(destination, format="PNG")


def main():
    spec = json.loads(Path("experiment_config.json").read_text(encoding="utf-8"))
    seed = spec["seed"]
    random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(2)
    root = os.environ["MNIST_ROOT"]
    training = MNIST(root=root, train=True, download=False, transform=ToTensor())
    testing = MNIST(root=root, train=False, download=False, transform=ToTensor())
    train_indices = torch.randperm(
        len(training), generator=torch.Generator().manual_seed(seed)
    )[:spec["train_samples"]].tolist()
    test_indices = torch.randperm(
        len(testing), generator=torch.Generator().manual_seed(seed + 1)
    )[:spec["test_samples"]].tolist()
    train_dataset = Subset(training, train_indices)
    test_dataset = Subset(testing, test_indices)
    test_loader = DataLoader(test_dataset, batch_size=spec["batch_size"], shuffle=False)
    output_dir = Path("outputs")
    checkpoint_dir = Path("checkpoints")
    output_dir.mkdir(exist_ok=True)
    checkpoint_dir.mkdir(exist_ok=True)
    runs = []
    for name, constructor in (("mlp", MLP), ("cnn", CNN)):
        torch.manual_seed(seed)
        model = constructor()
        optimizer = torch.optim.Adam(model.parameters(), lr=spec["learning_rate"])
        criterion = nn.CrossEntropyLoss()
        loader = DataLoader(
            train_dataset,
            batch_size=spec["batch_size"],
            shuffle=True,
            generator=torch.Generator().manual_seed(seed),
        )
        losses = []
        accuracies = []
        started = time.monotonic()
        for epoch in range(spec["epochs"]):
            model.train()
            total_loss = 0.0
            correct = 0
            count = 0
            for images, targets in loader:
                optimizer.zero_grad(set_to_none=True)
                logits = model(images)
                loss = criterion(logits, targets)
                if not torch.isfinite(loss):
                    raise RuntimeError("NaN training loss")
                loss.backward()
                optimizer.step()
                total_loss += loss.item() * len(targets)
                correct += (logits.argmax(dim=1) == targets).sum().item()
                count += len(targets)
            losses.append(total_loss / count)
            accuracies.append(correct / count)
            print(
                f"{name} epoch={epoch + 1} loss={losses[-1]:.4f} "
                f"train_accuracy={accuracies[-1]:.4f}",
                flush=True,
            )
        model.eval()
        correct = 0
        with torch.no_grad():
            for images, targets in test_loader:
                correct += (model(images).argmax(dim=1) == targets).sum().item()
        test_accuracy = correct / len(test_dataset)
        duration = time.monotonic() - started
        torch.save(model.state_dict(), checkpoint_dir / f"{name}.pt")
        runs.append({
            "model": name,
            "train_loss": losses,
            "train_accuracy": accuracies,
            "test_accuracy": test_accuracy,
            "parameters": sum(parameter.numel() for parameter in model.parameters()),
            "duration_seconds": duration,
        })
        print(f"{name} test_accuracy={test_accuracy:.4f}", flush=True)
    payload = {
        "schema_version": 1,
        "dataset": "mnist",
        "seed": seed,
        "train_samples": len(train_dataset),
        "test_samples": len(test_dataset),
        "runs": runs,
    }
    (output_dir / "raw_metrics.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    plot_series(runs, "train_loss", output_dir / "loss.png")
    plot_series(runs, "train_accuracy", output_dir / "accuracy.png")


if __name__ == "__main__":
    main()
