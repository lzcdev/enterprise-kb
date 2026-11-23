"""Docling 解析器：本地生产档主力，结构感知 + 表格识别 + 溯源坐标。

同样实现 Parser 协议，和 PyMuPDFParser 接口完全一致 —— 这就是可拔插：
工厂里换一个 backend 配置，业务代码零改动。

Docling 相比 PyMuPDF 多做什么：
- DocLayNet 版面分析：识别标题、段落、表格、图片的区域
- TableFormer：把表格还原成结构化的行列表，不会切碎
- 输出 DoclingDocument：每个元素带页码和 bbox 坐标

为什么用懒加载 + 优雅降级：
Docling 依赖 torch，体积大、安装慢。原型阶段不该被它拖住。
所以 import 失败不抛异常，只有真正调用 parse() 且环境没装时才给出清晰提示。
"""
from pathlib import Path

from app.interfaces import Parser, ParsedSection


class DoclingParser:
    name = "docling"

    def parse(self, path: str) -> list[ParsedSection]:
        suffix = Path(path).suffix.lower()
        # Docling 主攻 PDF/DOCX；纯文本走简单通道即可，没必要动用版面模型
        if suffix in (".md", ".txt"):
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            return [ParsedSection(
                text=text,
                source_type="md" if suffix == ".md" else "txt",
            )] if text.strip() else []

        try:
            from docling.document_converter import DocumentConverter
        except ImportError as e:
            raise RuntimeError(
                "Docling 未安装。请先 `pip install docling==2.37.0`，"
                "或把 PARSER_BACKEND 改为 pymupdf 走快速通道。"
            ) from e

        converter = DocumentConverter()
        result = converter.convert(path)
        doc = result.document

        sections: list[ParsedSection] = []
        # Docling 的 export_to_dict 里每个 item 带 label 和页码，
        # 这里做简化版：按页聚合文本，并尽量保留标题。
        for item in doc.iterate_items():
            element, _ = item
            text = getattr(element, "text", "")
            if not text or not text.strip():
                continue
            label = getattr(element, "label", "")
            # 标题类元素记入 title_path，正文则带着标题上下文
            title_path = text.strip() if "title" in str(label).lower() else ""
            page_no = self._page_of(element)
            sections.append(ParsedSection(
                text=text,
                source_type="pdf" if suffix == ".pdf" else "docx",
                page=page_no,
                title_path=title_path,
                metadata={"label": str(label)},
            ))
        return sections

    @staticmethod
    def _page_of(element) -> int | None:
        """从 Docling 元素的 provenance 里取页码，取不到返回 None。"""
        try:
            prov = element.provenance
            if prov and getattr(prov, "page_no", None):
                return int(prov.page_no)
        except (AttributeError, TypeError, ValueError):
            pass
        return None
