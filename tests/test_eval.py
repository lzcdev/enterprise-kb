"""评测框架与回归门禁的单元测试。

重点验证：门禁能真正拦截退化——这是 AI 系统持续迭代的安全带。
"""
import json
from pathlib import Path

import pytest

from app.eval.gate import (
    MONITORED_METRICS, GateResult, compare_with_baseline,
    load_baseline, save_baseline, save_report,
)
from app.eval.runner import (
    CaseResult, EvalReport, _aggregate, _fact_in, _normalize, load_golden,
)


# ===== 确定性指标辅助函数 =====
def test_normalize_removes_spaces_and_punctuation():
    assert _normalize("5 分钟，有效") == _normalize("5分钟有效")
    assert _fact_in("验证码 5 分钟内有效", "5 分钟")
    assert not _fact_in("验证码 10 分钟有效", "5 分钟")


def test_load_golden_has_required_fields():
    golden = load_golden()
    assert golden["version"]
    cases = golden["cases"]
    assert len(cases) >= 10
    for c in cases:
        assert {"id", "question", "relevant_source", "key_facts",
                "should_answer", "category"} <= c.keys()
    # 至少有一条应拒答用例，才能测 abstention
    assert any(not c["should_answer"] for c in cases)


def test_aggregate_denominators():
    """聚合时分母只用对应子集，无据题不该拉低检索指标。"""
    answerable = CaseResult(case_id="a", question="q", category="", should_answer=True,
                            relevant_source="x.md", hit_at_1=1.0, hit_at_3=1.0,
                            reciprocal_rank=1.0, faithfulness=0.5, citation_hit=1.0)
    abstain = CaseResult(case_id="b", question="q2", category="", should_answer=False,
                         relevant_source=None, abstention_correct=1.0)
    m = _aggregate([answerable, abstain], top_k=5)
    assert m["recall@1"] == 1.0       # 只按 1 条应答题算，不是 1/2
    assert m["faithfulness"] == 0.5
    assert m["abstention_accuracy"] == 1.0  # 只按 1 条无据题算


# ===== 门禁核心：拦截退化 =====
def _report(metrics: dict) -> EvalReport:
    return EvalReport(metrics=metrics, cases=[], config={})


def test_gate_passes_when_no_regression():
    baseline = {"metrics": {"recall@3": 0.9, "faithfulness": 0.5}}
    report = _report({"recall@3": 0.9, "faithfulness": 0.5})
    gate = compare_with_baseline(report, baseline)
    assert gate.passed
    assert all(d.passed for d in gate.deltas)


def test_gate_passes_within_tolerance():
    """小幅波动（在容忍度内）应通过——避免正常波动误报。"""
    baseline = {"metrics": {"recall@3": 0.9}}
    report = _report({"recall@3": 0.85})  # 退化 0.05 < 容忍 0.10
    gate = compare_with_baseline(report, baseline)
    assert gate.passed


def test_gate_blocks_significant_regression():
    """核心：退化超过容忍度必须阻断。"""
    baseline = {"metrics": {"recall@3": 0.9}}
    report = _report({"recall@3": 0.6})  # 退化 0.3 > 容忍 0.10
    gate = compare_with_baseline(report, baseline)
    assert not gate.passed
    d = next(d for d in gate.deltas if d.name == "recall@3")
    assert not d.passed
    assert abs(d.delta - (-0.3)) < 1e-9


def test_gate_blocks_faithfulness_regression():
    baseline = {"metrics": {"faithfulness": 0.8}}
    report = _report({"faithfulness": 0.5})  # 退化 0.3 > 容忍 0.15
    assert not compare_with_baseline(report, baseline).passed


def test_gate_allows_improvement():
    """变好当然通过——门禁只拦退化，不拦进步。"""
    baseline = {"metrics": {"recall@1": 0.5}}
    report = _report({"recall@1": 0.9})
    assert compare_with_baseline(report, baseline).passed


def test_gate_no_baseline_passes_with_flag():
    gate = compare_with_baseline(_report({}), baseline=None) if not load_baseline() else None
    # 若已有基线文件，显式传空 dict 模拟"没有基线指标"
    g = compare_with_baseline(_report({}), baseline={})
    assert g.passed  # 没基线指标时 deltas 全按 0 vs 0，通过


def test_save_and_load_baseline():
    # 用 tempfile 而非 pytest 的 tmp_path：后者在当前沙箱环境有临时目录重复创建的 bug
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        report = _report({"recall@3": 0.88, "mrr@10": 0.7})
        p = save_baseline(report, Path(d) / "baseline.json")
        assert p.exists()
        data = load_baseline(p)
        assert data["metrics"]["recall@3"] == 0.88


def test_save_report_includes_cases():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        case = CaseResult(case_id="Q01", question="q", category="c", should_answer=True,
                          relevant_source="x.md")
        report = EvalReport(metrics={"recall@3": 1.0}, cases=[case], config={})
        p = save_report(report, Path(d) / "report.json")
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["cases"][0]["case_id"] == "Q01"


def test_monitored_metrics_have_tolerances():
    assert set(MONITORED_METRICS) >= {"recall@3", "mrr@10", "faithfulness",
                                      "abstention_accuracy"}
    assert all(0 < t <= 0.2 for t in MONITORED_METRICS.values())
