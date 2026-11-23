"""PyMuPDF 解析器：快速原型通道，处理 PDF/DOCX/MD/TXT。

实现 interfaces.py 里的 Parser 协议（结构化子类型，无需显式继承）。

为什么它是"快速通道"：
- 纯文本 PDF 解析极快，无需加载版面分析模型
- 代价：没有表格结构、没有标题层级识别（这些是 Docling 的强项）
- 所以它适合原型阶段跑通链路，生产档用 loader_docling.py

关键：每个 ParsedSection 都带 page（页码）——这是"答案可溯源"的数据基础。
"""
import re
from pathlib import Path

from app.interfaces import Parser, ParsedSection


class PyMuPDFParser:
    """实现 Parser 协议：提供 name 属性和 parse() 方法即可，无需继承。"""

    name = "pymupdf"

    def parse(self, path: str) -> list[ParsedSection]:
        suffix = Path(path).suffix.lower()
        if suffix == ".pdf":
            sections = self._parse_pdf(path)
        elif suffix == ".docx":
            sections = self._parse_docx(path)
        elif suffix in (".md", ".txt"):
            sections = self._parse_text(path)
        else:
            raise ValueError(f"不支持的文件格式: {suffix}")
        # 溯源字段在解析层注入：parser 唯一知道文件路径的地方。
        # 若放到建库脚本里补，任何绕过该脚本的调用（如测试/演示）都会丢失 filename，
        # 最终溯源显示 "unknown" —— 契约字段要在数据诞生的源头填充。
        filename = Path(path).name
        for s in sections:
            s.metadata.setdefault("filename", filename)
        return sections

    def _parse_pdf(self, path: str) -> list[ParsedSection]:
        import fitz  # PyMuPDF，懒加载

        doc = fitz.open(path)
        sections: list[ParsedSection] = []
        for page_no, page in enumerate(doc, start=1):  # 页码从 1 开始，符合人类直觉
            text = page.get_text("text")
            if text.strip():
                sections.append(ParsedSection(
                    text=text,
                    source_type="pdf",
                    page=page_no,
                ))
        doc.close()
        return sections

    def _parse_docx(self, path: str) -> list[ParsedSection]:
        from docx import Document  # python-docx，懒加载

        doc = Document(path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        # DOCX 没有页码概念，整篇合成一段；切分交给 splitter
        return [ParsedSection(
            text="\n".join(paragraphs),
            source_type="docx",
        )] if paragraphs else []

    def _parse_text(self, path: str) -> list[ParsedSection]:
        # MD/TXT 用 UTF-8 读取，errors="replace" 防止个别乱码字符中断整个文件
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        suffix = Path(path).suffix.lower()
        if suffix == ".md":
            return self._parse_markdown_by_heading(text)  # MD 按标题分节，保住 title_path
        return [ParsedSection(text=text, source_type="txt")] if text.strip() else []

    def _parse_markdown_by_heading(self, text: str) -> list[ParsedSection]:
        """按 Markdown 标题层级分节，把当前标题路径写进每个 section 的 title_path。

        这是"按文档结构切分"的轻量实现：标题是天然的最强语义边界，
        而且把标题挂到 metadata 上，检索时能当过滤条件、生成时能当上下文。
        """
        sections: list[ParsedSection] = []
        heading_stack: dict[int, str] = {}  # 层级 -> 标题，如 {1: 产品手册, 2: 退款政策}
        buffer: list[str] = []
        current_title = ""

        def flush():
            nonlocal buffer
            content = "\n".join(buffer).strip()
            if content:
                sections.append(ParsedSection(
                    text=content,
                    source_type="md",
                    title_path=current_title,
                ))
            buffer = []

        for line in text.splitlines():
            match = re.match(r"^(#{1,6})\s+(.*)", line)
            if match:
                flush()  # 遇到新标题，先把上一节存起来
                level = len(match.group(1))
                heading_stack[level] = match.group(2).strip()
                # 丢弃更深层级残留的标题，避免 "退款政策 > 旧小节" 这种脏路径
                for deeper in [k for k in heading_stack if k > level]:
                    del heading_stack[deeper]
                current_title = " > ".join(heading_stack[k] for k in sorted(heading_stack))
            else:
                buffer.append(line)
        flush()
        return sections
