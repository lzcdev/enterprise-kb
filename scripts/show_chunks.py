"""演示脚本：直观看到「一份文档 → 切好的 chunks」的完整过程。

为什么要有这个脚本？
    之前让你敲的 python3 -c "..." 单行命令又长又有引号/方括号，
    shell 很容易解析出错（甚至误生成文件）。把逻辑固化成脚本，
    你只需要运行一行：.venv/bin/python scripts/show_chunks.py

用法（都在项目根目录执行）：
    .venv/bin/python scripts/show_chunks.py
        -> 默认切分 data/测试产品手册.md

    .venv/bin/python scripts/show_chunks.py data/客服FAQ.md
        -> 切分指定文件

    .venv/bin/python scripts/show_chunks.py --size 80 --overlap 10
        -> 调小 chunk_size，观察 chunk 数量变化

    .venv/bin/python scripts/show_chunks.py --sections
        -> 额外显示中间产物 ParsedSection（看数据流的每一跳）
"""
import argparse
import sys
from pathlib import Path

# 坑：直接运行 scripts/xxx.py 时，Python 把 scripts/ 加入 sys.path 而非项目根目录，
# 导致 import app 失败。所以手动把项目根目录（本项目 scripts/ 的上一级）注入。
# 这样你从任意目录执行这个脚本都能跑通。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 演示阶段固定用 PyMuPDF 解析器：它已装好，能处理 PDF/DOCX/MD/TXT 全部格式。
# Docling 是"生产档"（版面分析 + 表格识别），装好后可通过 factory.get_parser() 切换，
# 但那时 PARSER_BACKEND=docling，本脚本演示的是已跑通的链路，所以直接指定。
from app.ingestion.loader_pymupdf import PyMuPDFParser
from app.ingestion.splitter import RecursiveSplitter, section_to_chunks


def main() -> None:
    parser_cli = argparse.ArgumentParser(description="查看文档切分结果")
    parser_cli.add_argument("file", nargs="?", default="data/测试产品手册.md",
                            help="要切分的文档路径（默认 data/测试产品手册.md）")
    parser_cli.add_argument("--size", type=int, default=150, help="chunk 大小，单位字符（默认 150）")
    parser_cli.add_argument("--overlap", type=int, default=20, help="相邻块重叠字符数（默认 20）")
    parser_cli.add_argument("--sections", action="store_true",
                            help="同时显示阶段1的中间产物 ParsedSection")
    args = parser_cli.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在: {path}")
        print("当前 data/ 目录下的文件：")
        for p in sorted(Path("data").glob("*")):
            if p.is_file():
                print("   ", p.name)
        return

    parser = PyMuPDFParser()
    splitter = RecursiveSplitter(chunk_size=args.size, overlap=args.overlap)

    print("=" * 62)
    print(f"文件: {path}    chunk_size={args.size}    overlap={args.overlap}")
    print("=" * 62)

    # ===== 阶段 1：解析（文件 -> ParsedSection）=====
    sections = parser.parse(str(path))
    for s in sections:
        s.metadata.setdefault("filename", path.name)
    print(f"[阶段1 解析] 得到 {len(sections)} 个 ParsedSection")
    if args.sections:
        for i, s in enumerate(sections, 1):
            title = s.title_path or "(无标题)"
            print(f"    section {i}: page={s.page}  标题={title!r}  字数={len(s.text)}")

    # ===== 阶段 2：切分（ParsedSection -> Chunk）=====
    chunks: list = []
    for s in sections:
        chunks.extend(section_to_chunks(s, splitter))
    print(f"[阶段2 切分] 得到 {len(chunks)} 个 Chunk")
    print("=" * 62)

    # ===== 展示每个 chunk =====
    for i, c in enumerate(chunks, 1):
        loc = []
        if c.meta.page:
            loc.append(f"第{c.meta.page}页")
        if c.meta.title_path:
            loc.append(c.meta.title_path)
        where = " | ".join(loc) if loc else "无位置信息"
        print(f"\n--- chunk {i}  [{where}]  字数={len(c.text)} ---")
        print(c.text)


if __name__ == "__main__":
    main()
