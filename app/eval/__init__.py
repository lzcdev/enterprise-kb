"""评测子包：RAG 效果评测 + 回归门禁。

与 pytest（测工程正确性）互补：这里测的是【语义质量】，
且刻意做成不依赖 API key、不依赖真模型的确定性评测，
让原型档也能建立可复现的质量基线和回归门禁。
"""
from app.eval.runner import EvalReport, evaluate, load_golden
from app.eval.gate import GateResult, compare_with_baseline

__all__ = ["EvalReport", "evaluate", "load_golden", "GateResult", "compare_with_baseline"]
