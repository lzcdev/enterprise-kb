"""输入校验：问题长度、空值、prompt injection 基础防护。

这是防幻觉/防攻击的第一道门——在请求进入 RAG 流水线之前就拦掉明显恶意或无效的输入。

注意边界：prompt injection 没有银弹。这里做的是"基础启发式检测"，
拦的是最常见的直白注入串。真正稳健的防护要靠多层：
输入侧检测 + 系统提示词隔离 + 输出侧引用校验 + 权限最小化（LLM 不能调高危工具）。
面试要诚实说清这是纵深防御的一层，不是万能盾。
"""
import re

MAX_QUESTION_LENGTH = 1000

# 常见的直白注入模式：试图让模型丢弃系统指令、扮演无限制角色、泄露 prompt。
# 用正则而非精确匹配，覆盖大小写和常见变体。
INJECTION_PATTERNS = [
    r"忽略(上面|之前|以上|所有).*(指令|提示|要求|规则)",
    r" disregard .*(instruction|prompt|rule)",
    r"ignore .*(previous|above|all).*(instruction|prompt)",
    r"( Exposure|泄露|输出|重复).*(system prompt|系统提示|上面.*指令)",
    r"developer mode",
    r"jailbreak",
    r"你现在是.*(不受限制|无限制|没有限制)",
]


class ValidationError(Exception):
    """输入不合法。API 层映射成 400。"""


def validate_question(question: str) -> str:
    """校验问题，返回清洗后的文本；不合法抛 ValidationError。"""
    if question is None or not question.strip():
        raise ValidationError("问题不能为空")
    q = question.strip()
    if len(q) > MAX_QUESTION_LENGTH:
        raise ValidationError(f"问题过长：{len(q)} 字符，上限 {MAX_QUESTION_LENGTH}")
    lowered = q.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lowered):
            raise ValidationError("检测到疑似提示注入，请求被拒绝")
    return q
