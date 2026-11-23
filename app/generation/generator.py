"""生成层：拼 prompt -> 调 LLM -> 解析引用 -> 产出带溯源的答案。

设计要点（面试可讲）：
1. build_prompt 与 LLM 实现解耦：prompt 是纯函数，可单独测试、单独调优，
   换 LLM 不影响 prompt，换 prompt 不影响 LLM。
2. 引用溯源：prompt 要求 LLM 用 [1][2] 标注来源，生成后用正则解析出引用编号，
   映射到对应 chunk 的 metadata 生成 Source —— to B 产品"答案可追溯"的硬需求。
3. 空结果不调 LLM：由上层 RAGService 保证（见 rag_service.py），
   空上下文喂给 LLM 只会得到编造的答案。

MockLLM 说明：原型档零依赖实现，做"抽取式"回答（从证据里挑最相关的一句并标注引用），
不调用任何大模型。它只验证控制流，不展示真实生成质量。
"""
import re
from typing import Optional

from app.interfaces import LLM, ScoredChunk
from app.models import Source

# Prompt 模板：规则 / 证据 / 问题 三段式。
# 规则里明确"只据资料回答 + 标注引用 + 资料没有就说不知道"，是防幻觉第二道闸门。
PROMPT_TEMPLATE = """你是一个企业知识库问答助手。请严格根据下面的【参考资料】回答问题。

规则：
1. 只使用参考资料中的信息，不要使用你自己的知识，不要编造。
2. 如果参考资料不足以回答，直接说"知识库中没有相关信息"，不要猜测。
3. 在 answer 中使用 [编号] 标注引用的资料，例如：退款需7天[1]。
4. 用简洁的中文回答。

参考资料：
{context}

问题：{question}

请给出回答："""


def build_prompt(question: str, candidates: list[ScoredChunk]) -> str:
    """把候选 chunk 编号后拼进 prompt。编号从 1 开始，与 Source 溯源一一对应。"""
    lines = [
        f"[{i}] {c.text}" + (f"（来源：{c.metadata.get('filename', '')} "
                            f"第{c.metadata.get('page')}页）" if c.metadata.get("page") else "")
        for i, c in enumerate(candidates, 1)
    ]
    return PROMPT_TEMPLATE.format(context="\n".join(lines), question=question)


def verify_citations(answer: str, candidates: list[ScoredChunk]) -> tuple[bool, list[int]]:
    """校验答案里的引用编号：是否合法 + 编号对应的证据是否与答案有实质重叠。

    为什么需要？
        LLM 可能编造一个不存在的 [9]（越界引用），或者标了 [1] 但答案内容
        和 [1] 的证据毫无关系（张冠李戴）。to B 场景答案要能点开原文核对，
        假引用会直接损害可信度。这是防幻觉的又一道闸门。

    实现思路（启发式，非语义）：
        1. 越界检查：编号必须在 1..len(candidates) 内。
        2. 重叠检查：去掉停用标点后，答案与被引 chunk 的字符交集比例。
           用字符级 2-gram 交集，比单词更适合中文（无需分词）。
        阈值 0.1 是宽松基线：只拦"完全没关系"的假引用，不误伤正常引用。
        生产应换成 NLI 模型或让 judge LLM 判断，成本更高但更准。

    返回：(是否全部通过, 通过校验的编号列表)
    """
    nums = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)})
    if not nums:
        return True, []  # 没标引用不算错（有些答案不需要引用）

    # 答案正文（去掉引用标记和空白标点），转 2-gram 集合
    body = re.sub(r"\[\d+\]", "", answer)
    body_clean = re.sub(r"[\s，。！？、；：\"'（）()《》]", "", body)
    if len(body_clean) < 2:
        body_grams = {body_clean}
    else:
        body_grams = {body_clean[i:i + 2] for i in range(len(body_clean) - 1)}

    valid: list[int] = []
    for n in nums:
        if not (1 <= n <= len(candidates)):
            continue  # 越界引用：直接丢弃
        evidence = re.sub(r"[\s，。！？、；：\"'（）()《》]", "", candidates[n - 1].text)
        if len(evidence) < 2:
            ev_grams = {evidence}
        else:
            ev_grams = {evidence[i:i + 2] for i in range(len(evidence) - 1)}
        if not body_grams or not ev_grams:
            continue
        overlap = len(body_grams & ev_grams) / len(body_grams)
        if overlap >= 0.1:
            valid.append(n)

    return len(valid) == len(nums), valid


def parse_citations(answer: str, candidates: list[ScoredChunk],
                    *, verify: bool = False) -> list[Source]:
    """从答案里提取 [n]，映射回 chunk 生成 Source 列表（去重、按引用顺序）。

    只返回 LLM 真正引用到的 chunk —— 不是把全部候选都塞给前端，
    这样溯源列表和用户看到的证据一致。

    verify=True 时先用 verify_citations 过滤掉越界/无关引用，只保留可信的。
    默认 False 保持向后兼容（老测试不受影响），RAGService 里显式开 True。
    """
    if verify:
        _, nums = verify_citations(answer, candidates)
    else:
        nums = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)})
    sources: list[Source] = []
    for n in nums:
        if 1 <= n <= len(candidates):
            c = candidates[n - 1]
            md = c.metadata
            sources.append(Source(
                doc_id=md.get("doc_id", c.id),
                filename=md.get("filename", ""),
                page=md.get("page"),
                title_path=md.get("title_path", ""),
                text=c.text,
                score=c.score,
                start_ms=md.get("start_ms"),
                end_ms=md.get("end_ms"),
            ))
    return sources


class OpenAILikeLLM:
    """真 LLM：OpenAI 兼容接口（DeepSeek/通义/OpenAI）。懒加载，优雅降级。"""

    def __init__(self, api_key: str = "", base_url: str = "https://api.deepseek.com/v1",
                 model: str = "deepseek-chat"):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.model_id = f"api-{model}"
        self._client = None

    def _load(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("生成需要 openai SDK：pip install openai") from exc
            if not self.api_key:
                raise RuntimeError("未配置 LLM_API_KEY，无法调用大模型。"
                                   "原型档可改 LLM_BACKEND=mock 零依赖跑通链路。")
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def generate(self, prompt: str, *, max_tokens: int = 1024) -> str:
        resp = self._load().chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.1,  # 知识库问答要稳定，别发挥
        )
        return resp.choices[0].message.content or ""


class MockLLM:
    """零依赖抽取式回答：从证据里挑与问题字面重叠最多的一句，标注引用。

    只用于原型档验证"prompt->生成->引用解析"控制流，无语义理解、非真实生成质量。
    """

    model_id = "mock-extractive-v1"

    def generate(self, prompt: str, *, max_tokens: int = 1024) -> str:
        # 从 prompt 里解析问题与编号证据（不重新实现语义，只做字符串处理）
        q_match = re.search(r"问题：(.*?)\n", prompt, re.S)
        question = q_match.group(1).strip() if q_match else ""
        evidences = re.findall(r"^\[(\d+)\]\s+(.*?)(?=\n\[\d+\]|\n\n请给出回答|\Z)",
                               prompt, re.M | re.S)
        if not evidences:
            return "知识库中没有相关信息"

        def overlap(text: str) -> int:
            return sum(1 for ch in set(question) if ch in text and ch.strip())

        best_num, best_text = max(evidences, key=lambda e: overlap(e[1]))
        # 抽取式：截取证据首句作为答案，并标注引用编号
        first_sentence = re.split(r"[。！？\n]", best_text.strip())[0]
        return f"{first_sentence}。[{best_num}]"


def get_llm(backend: str = "mock", *, api_key: str = "",
            base_url: str = "https://api.deepseek.com/v1",
            model: str = "deepseek-chat") -> Optional[LLM]:
    """按 backend 选生成实现。"""
    if backend == "api":
        return OpenAILikeLLM(api_key=api_key, base_url=base_url, model=model)
    if backend == "mock":
        return MockLLM()
    if backend == "off":
        return None
    raise ValueError(f"未知的 LLM_BACKEND: {backend}")
