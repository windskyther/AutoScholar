# ruff: noqa: RUF001
from dataclasses import dataclass
from typing import Any

from autoscholar.experiment.models import ExperimentSpecification, RawExperimentMetrics


class ExperimentMetricsError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "experiment_metrics_invalid"
        self.message = message


@dataclass(frozen=True, slots=True)
class ExperimentAnalysis:
    metrics: dict[str, Any]
    report: str


def analyze_experiment(
    specification: ExperimentSpecification, raw: RawExperimentMetrics
) -> ExperimentAnalysis:
    if (
        raw.dataset != specification.dataset
        or raw.seed != specification.seed
        or raw.train_samples != specification.train_samples
        or raw.test_samples != specification.test_samples
        or any(len(run.train_loss) != specification.epochs for run in raw.runs)
    ):
        raise ExperimentMetricsError("Measured dataset, seed, sample count or epoch count differs")

    mlp, cnn = raw.runs
    delta = cnn.test_accuracy - mlp.test_accuracy
    winner = "cnn" if delta > 0 else "mlp" if delta < 0 else "tie"
    metrics: dict[str, Any] = {
        "schema_version": 1,
        "dataset": raw.dataset,
        "seed": raw.seed,
        "train_samples": raw.train_samples,
        "test_samples": raw.test_samples,
        "epochs": specification.epochs,
        "batch_size": specification.batch_size,
        "learning_rate": specification.learning_rate,
        "runs": [run.model_dump() for run in raw.runs],
        "winner": winner,
        "cnn_minus_mlp_accuracy": delta,
        "comparison_scope": "fixed MNIST subset, engineering acceptance",
    }
    winner_text = "两者测试准确率相同" if winner == "tie" else f"{winner.upper()} 测试准确率较高"
    report = (
        "# MNIST MLP 与 CNN 实验对比\n\n"
        "## 实验设置\n\n"
        f"- 数据集：MNIST；训练样本 {raw.train_samples}，测试样本 {raw.test_samples}\n"
        f"- 随机种子：{raw.seed}；Epoch：{specification.epochs}；"
        f"Batch size：{specification.batch_size}\n"
        f"- 学习率：{specification.learning_rate}；设备：CPU\n\n"
        "## 实测结果\n\n"
        "| 模型 | 测试准确率 | 参数量 | 训练耗时（秒） | 最终训练损失 |\n"
        "|---|---:|---:|---:|---:|\n"
        f"| MLP | {mlp.test_accuracy:.2%} | {mlp.parameters} | "
        f"{mlp.duration_seconds:.2f} | {mlp.train_loss[-1]:.4f} |\n"
        f"| CNN | {cnn.test_accuracy:.2%} | {cnn.parameters} | "
        f"{cnn.duration_seconds:.2f} | {cnn.train_loss[-1]:.4f} |\n\n"
        "## 结论与限制\n\n"
        f"{winner_text}，CNN 减 MLP 的准确率差为 {delta:+.2%}。"
        "损失与训练准确率曲线分别见 loss.png 和 accuracy.png。\n\n"
        "该结果基于固定子集和有限训练轮次，只用于工程验收，"
        "不能视作完整 MNIST 精度基准。\n"
    )
    return ExperimentAnalysis(metrics=metrics, report=report)
