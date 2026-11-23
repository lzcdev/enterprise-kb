"""回归门禁：判断这次评测相比基线是否退化，决定能否进入下一环节。

核心思想（面试高频，务必讲透）：
    传统软件测试写死 assert output == expected，对错分明。
    但 RAG/LLM 输出是概率性的、指标会波动，写死阈值有两个问题：
      1. 绝对分数没有可比性——换数据集、换模型，分数基准全变。
      2. 每次改 prompt 都跑一遍，"80 分算好吗？"没有客观答案。
    所以工业界用的是【回归门禁】：保存一份基线分数，每次改动后重跑，
    只要关键指标相比基线退化超过容忍度就阻断——"别让事情变坏"比"达到完美"更可执行。

门禁规则：
    对每个受监控指标，current >= baseline - tolerance 才通过。
    tolerance 按指标性质设定：核心检索指标（recall/mrr）容差小，
    因为退化 0.1 就意味着每 10 题多错 1 题，是可感知的质量下降。

CI 集成：`evaluate_and_gate()` 返回 GateResult，passed=False 时脚本 exit(1)，
流水线据此阻断合并。这就是 AI 系统能持续迭代又不越改越差的机制。
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.config import settings
from app.eval.runner import EvalReport


# 受门禁监控的指标 + 各自容忍的退化幅度。
# 不监控 elapsed_ms（机器快慢不同）、indexed_chunks（数据量变化）这类环境相关值。
MONITORED_METRICS: dict[str, float] = {
    "recall@1": 0.10,
    "recall@3": 0.10,
    "recall@5": 0.10,
    "mrr@10": 0.10,
    "faithfulness": 0.15,
    "citation_hit_rate": 0.15,
    "abstention_accuracy": 0.15,
}

BASELINE_PATH = settings.DATA_DIR / "eval" / "baseline.json"
REPORT_PATH = settings.DATA_DIR / "eval" / "last_report.json"


@dataclass
class MetricDelta:
    """单个指标的对比结果。"""
    name: str
    baseline: float
    current: float
    tolerance: float
    delta: float = 0.0
    passed: bool = True

    @property
    def status(self) -> str:
        return "✓" if self.passed else "✗ 退化"


@dataclass
class GateResult:
    """门禁判定结果。"""
    passed: bool
    deltas: list[MetricDelta] = field(default_factory=list)
    baseline_missing: bool = False

    def summary(self) -> str:
        lines = ["=" * 68, "回归门禁（对比基线，不退化即通过）", "=" * 68]
        if self.baseline_missing:
            lines.append("  基线不存在——本次结果可作为基线，门禁跳过")
        for d in self.deltas:
            lines.append(
                f"  {d.status:<6} {d.name:<22} "
                f"基线 {d.baseline:.4f} → 当前 {d.current:.4f} "
                f"(Δ{d.delta:+.4f}, 容忍 -{d.tolerance:.2f})"
            )
        lines.append("-" * 68)
        lines.append("  结论: " + ("✓ 通过，可合并/部署" if self.passed else "✗ 阻断：质量退化超过容忍度"))
        lines.append("=" * 68)
        return "\n".join(lines)


def save_baseline(report: EvalReport, path: Optional[Path] = None) -> Path:
    """把当前评测结果存为新基线。

    注意：刷新基线是一次【显式决策】——确认质量变化是有意为之（如换了更好的模型）
    才做。门禁本身绝不自动刷新基线，否则"每次退化就把基线调低"等于没有门禁。
    """
    p = Path(path) if path is not None else BASELINE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metrics": report.metrics,
        "config": report.config,
        "dataset_version": report.dataset_version,
        "note": "基线记录：门禁对比的参照物。仅在确认质量变化有意为之时手动刷新。",
    }
    p.write_text(json_dumps(payload), encoding="utf-8")
    return p


def save_report(report: EvalReport, path: Optional[Path] = None) -> Path:
    """保存每次评测报告（即使没过门禁也留档，便于追溯质量变化趋势）。"""
    p = Path(path) if path is not None else REPORT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json_dumps(report.to_dict()), encoding="utf-8")
    return p


def load_baseline(path: Optional[Path] = None) -> Optional[dict]:
    p = Path(path) if path is not None else BASELINE_PATH
    if not p.exists():
        return None
    import json
    return json.loads(p.read_text(encoding="utf-8"))


def compare_with_baseline(report: EvalReport,
                          baseline: Optional[dict] = None) -> GateResult:
    """对比当前评测与基线，判定门禁通过与否。

    baseline=None 时会尝试从默认路径加载；仍不存在则 GateResult(passed=True,
    baseline_missing=True)——首次运行没有基线是正常情况，用当前结果建基线即可。
    """
    baseline = baseline if baseline is not None else load_baseline()
    if baseline is None:
        return GateResult(passed=True, baseline_missing=True)

    base_metrics = baseline.get("metrics", {})
    deltas: list[MetricDelta] = []
    for name, tolerance in MONITORED_METRICS.items():
        b = float(base_metrics.get(name, 0.0))
        c = float(report.metrics.get(name, 0.0))
        delta = c - b
        passed = delta >= -tolerance
        deltas.append(MetricDelta(
            name=name, baseline=b, current=c,
            tolerance=tolerance, delta=delta, passed=passed,
        ))
    return GateResult(passed=all(d.passed for d in deltas), deltas=deltas)


def json_dumps(obj) -> str:
    """统一 JSON 序列化（缩进 + 中文不转义），报告要给人看也要给机器读。"""
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)
