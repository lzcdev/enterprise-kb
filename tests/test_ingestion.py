"""解析与切分层的单元测试。

用 pytest 跑：python3 -m pytest tests/test_ingestion.py -v
测试的意义：解析切分是数据入口，这里的 bug 会污染后面所有环节（向量库、检索）。
所以必须用测试把行为固定下来，改代码时才知道有没有破坏既有逻辑。
"""
from app.ingestion.loader_pymupdf import PyMuPDFParser
from app.ingestion.splitter import RecursiveSplitter, split_document
from app.interfaces import Parser


def test_parser_implements_protocol():
    """验证 PyMuPDFParser 满足 Parser 协议（结构化子类型）。"""
    parser: Parser = PyMuPDFParser()  # 类型注解即校验
    assert parser.name == "pymupdf"
    assert hasattr(parser, "parse")


def test_markdown_heading_extraction():
    """ Markdown 按标题分节，title_path 要正确带层级。"""
    parser = PyMuPDFParser()
    sections = parser.parse("data/测试产品手册.md")
    assert len(sections) > 0
    # 应该能找到三级标题路径的那种 section
    paths = [s.title_path for s in sections]
    assert any("退款政策" in p and "七天无理由退款" in p for p in paths)


def test_chunk_size_respected():
    """每个 chunk 不能超过 chunk_size（标题前缀不严格计入，正文受控）。"""
    chunks = split_document(
        "data/测试产品手册.md",
        PyMuPDFParser(),
        RecursiveSplitter(chunk_size=100, overlap=10),
    )
    assert all(len(c.text) < 160 for c in chunks)  # 正文 100 + 标题前缀余量


def test_chunks_carry_source_metadata():
    """每个 chunk 必须带 source_type 和 filename，否则无法溯源。"""
    chunks = split_document("data/测试产品手册.md", PyMuPDFParser())
    assert all(c.meta.source_type == "md" for c in chunks)
    assert all(c.meta.filename == "测试产品手册.md" for c in chunks)
