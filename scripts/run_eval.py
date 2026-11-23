"""评测 + 回归门禁命令行入口。

用法：
    # 首次：跑评测并保存为基线
    .venv/bin/python scripts/run_eval.py --save-baseline

    # 日常/CI：跑评测并对比基线（退化超容忍度则 exit 1）
    .venv/bin/python scripts/run_eval.py

    # 看逐题明细（哪题没答对、召回了什么）
    .venv/bin/python scripts/run_eval.py --verbose

    # JSON 输出（CI 解析）
    .venv/bin/python scripts/run_eval.py --json

CI 用法（GitHub Actions 里就是一行）：
    .venv/bin/python scripts/run_eval.py   # 非 0 退出码自动让 workflow 失败
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.eval.gate import (
    compare_with_baseline, load_baseline, save_baseline, save_report,
)
from app.eval.runner import evaluate


def main() -> int:
    cli = argparse.ArgumentParser(description="RAG 评测与回归门禁")
    cli.add_argument("--save-baseline", action="store_true", help="把本次结果存为新基线")
    cli.add_argument("--verbose", action="store_true", help="打印逐题明细")
    cli.add_argument("--json", action="store_true", help="输出 JSON（CI 用）")
    cli.add_argument("--no-gate", action="store_true", help="只评测不做门禁对比")
    args = cli.parse_args()

    report = evaluate()
    saved_report = save_report(report)
    gate = None  # 初始化：--save-baseline / --no-gate 分支下没有门禁对象

    if args.save_baseline:
        p = save_baseline(report)
        if not args.json:
            print(report.summary())
            print(f"\n[基线] 已保存: {p}")
        gate_passed = True
    elif args.no_gate:
        gate = None
        gate_passed = True
    else:
        gate = compare_with_baseline(report)
        gate_passed = gate.passed

    if args.json:
        out = {
            "metrics": report.metrics,
            "passed": gate_passed,
            "gate": [vars(d) for d in gate.deltas] if gate and gate.deltas else [],
            "baseline_missing": gate.baseline_missing if gate else False,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(report.summary())
        if args.verbose:
            print("\n逐题明细:")
            for c in report.cases:
                tag = "应拒答" if not c.should_answer else "应回答"
                print(f"  [{c.case_id}] {tag:<5} R@3={c.hit_at_3:.0f} "
                      f"MRR={c.reciprocal_rank:.2f} 忠实={c.faithfulness:.2f} "
                      f"引用={c.citation_hit:.0f} 拒答={c.abstention_correct:.0f} | {c.question}")
                if args.verbose and c.retrieved_sources:
                    print(f"       召回: {c.retrieved_sources[:3]}")
        if gate is not None:
            print("\n" + gate.summary())
        print(f"\n报告已保存: {saved_report}")

    return 0 if gate_passed else 1


if __name__ == "__main__":
    sys.exit(main())
