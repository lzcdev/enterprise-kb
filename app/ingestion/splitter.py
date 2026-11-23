"""切分器：把 ParsedSection 切成带标题上下文的 Chunk。

实现策略：递归字符切分（LangChain 默认方案，不需要任何模型）。
原理：分隔符有优先级，先尝试用最高优先级（段落 \n\n）切，
切完还超长就用次一级（句子 。）接着切，直到每块小于目标长度。
分隔符优先级本质上是在近似"语义边界的强度"：段落 > 句子 > 词 > 字符。

中文必须显式加中文标点分隔符，否则会退化成按字符硬切。
"""
import re
from pathlib import Path

from app.config import settings
from app.interfaces import ParsedSection
from app.models import Chunk, DocumentMeta

# 分隔符优先级：从强语义边界到弱。中文标点必须显式列出。
SEPARATORS = ["\n\n", "\n", "。", "？", "！", "；", "，", "、", " ", ""]


class RecursiveSplitter:
    def __init__(self, chunk_size: int | None = None, overlap: int | None = None):
        self.chunk_size = chunk_size or settings.CHUNK_SIZE
        self.overlap = overlap if overlap is not None else settings.CHUNK_OVERLAP

    def split(self, text: str) -> list[str]:
        """递归切分 + 合并，输出一个个不超过 chunk_size 的文本块。"""
        # 第一步：按分隔符递归拆成语义片段
        pieces = self._split_recursive(text, SEPARATORS)
        # 第二步：把小片段合并到接近 chunk_size，并加 overlap
        return self._merge_with_overlap(pieces)

    def _split_recursive(self, text: str, separators: list[str]) -> list[str]:
        if not text:
            return []
        sep = separators[0]
        rest = separators[1:]

        if sep == "":  # 最后兜底：没有分隔符了，按字符硬切
            return [text[i:i + self.chunk_size] for i in range(0, len(text), self.chunk_size)]

        # 用当前分隔符切；用 re.split 保留标点本身（标点属于前一句）
        parts = [p for p in re.split(f"({re.escape(sep)})", text) if p]
        result: list[str] = []
        pending = ""
        for part in parts:
            candidate = pending + part
            if len(candidate) <= self.chunk_size:
                pending = candidate
            else:
                if pending:
                    result.append(pending)
                if len(part) > self.chunk_size and rest:
                    result.extend(self._split_recursive(part, rest))  # 还超长，用更低优先级分隔符再切
                    pending = ""
                else:
                    pending = part
        if pending:
            result.append(pending)
        return result

    def _merge_with_overlap(self, pieces: list[str]) -> list[str]:
        """把小片段打包到目标长度，相邻块保留 overlap 个字符防止边界语义被切断。"""
        if not pieces:
            return []
        chunks: list[str] = []
        current = ""
        for piece in pieces:
            if not current:
                current = piece
            elif len(current) + len(piece) <= self.chunk_size:
                current += piece
            else:
                chunks.append(current)
                # 取上一块的尾部作为下一块的开头（边界缓冲）
                tail = current[-self.overlap:] if self.overlap > 0 else ""
                current = tail + piece
        if current:
            chunks.append(current)
        return chunks


def section_to_chunks(section: ParsedSection, splitter: RecursiveSplitter | None = None) -> list[Chunk]:
    """把一个 ParsedSection 转成多个 Chunk，并把标题上下文拼进文本开头。

    为什么拼标题：只切正文的话，检索到的 chunk 脱离语境，LLM 不知道它属于哪一节。
    拼上 [标题 > 子标题] 前缀能显著提升检索命中率和答案质量。
    """
    splitter = splitter or RecursiveSplitter()
    chunks: list[Chunk] = []
    for piece in splitter.split(section.text):
        prefix = f"[{section.title_path}] " if section.title_path else ""
        chunks.append(Chunk(
            text=prefix + piece,
            meta=DocumentMeta(
                filename=section.metadata.get("filename", "unknown"),
                source_type=section.source_type,  # type: ignore[arg-type]
                page=section.page,
                title_path=section.title_path,
            ),
        ))
    return chunks


def split_document(path: str, parser, splitter: RecursiveSplitter | None = None) -> list[Chunk]:
    """完整流程：文件 -> parser 解析 -> splitter 切块。工厂注入 parser 实现。"""
    sections = parser.parse(path)
    filename = Path(path).name
    for s in sections:
        s.metadata.setdefault("filename", filename)
    result: list[Chunk] = []
    for section in sections:
        result.extend(section_to_chunks(section, splitter))
    return result
