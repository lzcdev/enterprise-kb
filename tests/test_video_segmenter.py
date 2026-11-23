"""切分策略单元测试：锁住固定窗口与场景策略的核心行为。"""
from app.config import settings
from app.ingestion.loader_video import MockExtractor, MockSceneDetector, VideoParser
from app.ingestion.video_segmenter import (
    FixedWindowSegmenter, SceneAwareSegmenter, SegmentationStrategy,
)


TRANSCRIPT = MockExtractor().transcribe("demo.mp4")  # 6 句 * 15s = 90s
CANDIDATES = MockSceneDetector().detect("demo.mp4")  # [5000, 12000, 25000]


def test_segmenter_protocol():
    assert isinstance(FixedWindowSegmenter(), SegmentationStrategy)
    assert isinstance(SceneAwareSegmenter(), SegmentationStrategy)


def test_fixed_window():
    points = FixedWindowSegmenter(30).segment([], TRANSCRIPT, 0, 0)
    assert points == [0, 30000, 60000, 90000]


def test_scene_short_merged_long_split():
    points = SceneAwareSegmenter().segment(
        CANDIDATES, TRANSCRIPT,
        settings.VIDEO_MAX_SEGMENT_MS, settings.VIDEO_MIN_SEGMENT_MS,
    )
    # 5s 短场景被合并 -> 边界 5000 消失；12s 片段 >= min 保留
    assert 5000 not in points
    assert 12000 in points
    # 25s-90s 是 65s 长场景，超过 60s max -> 被切成 2 段，切点在中间
    assert len(points) >= 3
    assert all(points[i + 1] - points[i] <= settings.VIDEO_MAX_SEGMENT_MS
               for i in range(len(points) - 1))
    assert all(points[i + 1] - points[i] >= settings.VIDEO_MIN_SEGMENT_MS
               for i in range(len(points) - 1))


def test_video_parser_inject_scene_strategy():
    """注入场景策略后，VideoParser 走新切点，且时间戳完整透传。"""
    parser = VideoParser(strategy=SceneAwareSegmenter())
    sections = parser.parse("data/demo.mp4")
    assert len(sections) >= 2
    assert sections[0].metadata["start_ms"] == 0
    assert sections[-1].metadata["end_ms"] == 90000
