"""VideoParser 骨架的单元测试。

验证三件事：
1. VideoParser 满足 Parser 协议（鸭子类型）
2. 时间轴对齐正确（按窗口聚合，时间戳写进 metadata）
3. 视频 section 能走通用的 section_to_chunks，时间戳透传到 DocumentMeta
"""
from app.interfaces import Parser
from app.ingestion.loader_video import VideoParser
from app.ingestion.splitter import section_to_chunks


def test_video_parser_satisfies_protocol():
    """鸭子类型：满足 name + parse 即为 Parser，无需显式继承。"""
    p = VideoParser()
    assert isinstance(p, Parser)
    assert p.name == "video"


def test_align_and_segment_with_timestamps():
    """按 30 秒窗口聚合，时间戳写进 metadata。"""
    p = VideoParser(segment_seconds=30)
    sections = p.parse("data/demo.mp4")
    assert len(sections) == 3  # 90 秒 / 30 秒窗口 = 3 段
    first = sections[0]
    assert first.source_type == "video"
    assert first.metadata["start_ms"] == 0
    assert first.metadata["end_ms"] == 30000
    assert "退款政策" in first.text


def test_video_section_through_generic_splitter():
    """视频 section 走通用切分入口，时间戳透传到 DocumentMeta。"""
    p = VideoParser(segment_seconds=30, caption_enabled=True)
    sections = p.parse("data/demo.mp4")
    chunks = []
    for s in sections:
        chunks.extend(section_to_chunks(s))
    assert len(chunks) >= 3
    assert all(c.meta.source_type == "video" for c in chunks)
    assert all(c.meta.start_ms is not None for c in chunks)
    # 开启画面描述后，文本里应出现 [画面] 标记
    assert any("[画面]" in c.text for c in chunks)
