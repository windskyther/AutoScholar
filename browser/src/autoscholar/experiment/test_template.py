# mypy: ignore-errors
"""Sandbox-only pytest template for model structure and optimizer updates."""

import torch
from torch import nn
from train import CNN, MLP


def test_model_shapes_and_gradients():
    torch.manual_seed(42)
    images = torch.randn(4, 1, 28, 28)
    labels = torch.tensor([0, 1, 2, 3])
    for constructor in (MLP, CNN):
        model = constructor()
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
        before = [parameter.detach().clone() for parameter in model.parameters()]
        output = model(images)
        assert output.shape == (4, 10)
        loss = nn.CrossEntropyLoss()(output, labels)
        assert torch.isfinite(loss)
        loss.backward()
        assert all(parameter.grad is not None for parameter in model.parameters())
        optimizer.step()
        assert any(
            not torch.equal(previous, parameter)
            for previous, parameter in zip(before, model.parameters(), strict=True)
        )
