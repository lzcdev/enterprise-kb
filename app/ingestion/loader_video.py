"""VideoParser：视频解析器骨架，接入 Parser 协议。

设计目标：让"音视频平滑升级"从概念变成可运行的骨架，同时不装任何重依赖也能演示。

核心设计（面试要能讲）：
1. 三路提取（抽帧/音轨转写/画面OCR）抽象成 Extractor 接口，
   默认实现用懒加载调用 ffmpeg/whisper/PaddleOCR，没装则给清晰提示——优雅降级。
2. 可注入 MockExtractor 提供模拟数据，让整条数据流在无重依赖环境下也能跑通、能测试。
   这就是"依赖倒置"：高层流程不依赖具体的重依赖实现。
3. 切分策略可拔插："在哪切"由 SegmentationStrategy 决定（固定窗口 / 场景边界优先），
   "填什么内容"由 _align 负责——两者解耦，换策略不用改聚合逻辑。
4. 时间轴对齐：三路产物都带时间戳，按策略给出的切点聚合成 ParsedSection，
   每个 section 的 metadata 带 start_ms/end_ms，最终支撑"答案 + 跳转播放"。

注意：本文件是骨架（skeleton）——真实提取逻辑用 TODO 标出接入点，
但数据流、契约、时间戳对齐、切分策略都是真实可跑的。
"""
from pathlib import Path
from typing import Protocol

from app.config import settings
from app.interfaces import Parser, ParsedSection
from app.ingestion.video_segmenter import (
    FixedWindowSegmenter, SceneAwareSegmenter, SceneDetector, SegmentationStrategy,
)


# ============ 提取层抽象：三路提取的统一契约 ============
class FrameExtractor(Protocol):
    """画面抽帧：视频 -> [(time_ms, 图像路径或帧对象), ...]"""
    def extract(self, path: str, fps: float) -> list[tuple[int, object]]: ...


class AudioTranscriber(Protocol):
    """音轨转写：视频 -> [(start_ms, end_ms, 文字), ...]"""
    def transcribe(self, path: str) -> list[tuple[int, int, str]]: ...


class FrameDescriber(Protocol):
    """画面描述（VLM）：帧 -> 文字描述。可选，成本最高。"""
    def describe(self, frames: list[tuple[int, object]]) -> list[tuple[int, str]]: ...


# ============ 默认实现：真实依赖的懒加载适配器（骨架）============
class FFmpegFrameExtractor:
    """用 ffmpeg 固定间隔抽帧。真实实现骨架。"""

    def extract(self, path: str, fps: float) -> list[tuple[int, object]]:
        # TODO 真实实现：subprocess 调 ffmpeg -vf fps={fps} 抽帧到临时目录，
        #      用 PySceneDetect 做场景切换检测可升级为智能抽帧。
        raise RuntimeError(
            "抽帧依赖未接入：需要系统安装 ffmpeg，或改用 PySceneDetect 做场景检测。"
            "演示请传入 MockExtractor。"
        )


class WhisperTranscriber:
    """用 faster-whisper 转写音轨，自带词级时间戳。真实实现骨架。"""

    def __init__(self, model_size: str = "small"):
        self.model_size = model_size

    def transcribe(self, path: str) -> list[tuple[int, int, str]]:
        # TODO 真实实现：faster-whisper 加载模型，转写后用 word_timestamps 对齐到毫秒。
        raise RuntimeError(
            "音轨转写依赖未接入：需要 pip install faster-whisper。"
            "演示请传入 MockExtractor。"
        )


class PaddleOCRDescriber:
    """画面 OCR + VLM 描述。真实实现骨架。"""

    def describe(self, frames: list[tuple[int, object]]) -> list[tuple[int, str]]:
        # TODO 真实实现：PaddleOCR 提取画面文字；如需语义描述再调 Qwen3-VL 生成 caption。
        raise RuntimeError(
            "画面描述依赖未接入：需要 PaddleOCR（+ 可选 Qwen3-VL）。"
            "演示请传入 MockExtractor。"
        )


class PySceneDetectDetector:
    """真实场景边界检测骨架：PySceneDetect / TransNetV2 接入点。"""

    def detect(self, path: str) -> list[int]:
        # TODO 真实实现：用 PySceneDetect 的 ContentDetector 检测硬切，
        #      ThresholdDetector 检测淡入淡出；渐变转场多的场景换 TransNetV2。
        #      返回毫秒级边界列表，再和音轨话题边界融合后传入 SceneAwareSegmenter。
        raise RuntimeError(
            "场景检测依赖未接入：需要 pip install scenedetect。"
            "演示请传入 MockSceneDetector。"
        )


# ============ Mock 适配器：无重依赖也能跑通全流程 ============
class MockExtractor(FrameExtractor, AudioTranscriber, FrameDescriber):
    """模拟三路提取产物，用于演示和单元测试。

    为什么需要它：让数据流（时间轴对齐→切片→ParsedSection）可以脱离重依赖独立验证。
    这是测试驱动开发里"用假实现替换慢/贵的依赖"的标准做法。
    """

    def extract(self, path: str, fps: float) -> list[tuple[int, object]]:
        # 模拟每 30 秒一帧的关键帧
        return [(0, "frame@0s"), (30000, "frame@30s"), (60000, "frame@60s"), (90000, "frame@90s")]

    def transcribe(self, path: str) -> list[tuple[int, int, str]]:
        return [
            (0, 15000, "大家好，今天讲退款政策。"),
            (15000, 30000, "七天无理由退款怎么申请。"),
            (30000, 45000, "自签收起七日内商品保持完好。"),
            (45000, 60000, "在订单详情页提交申请即可。"),
            (60000, 75000, "审核通过后三到五个工作日到账。"),
            (75000, 90000, "定制商品和生鲜不支持退款。"),
        ]

    def describe(self, frames: list[tuple[int, object]]) -> list[tuple[int, str]]:
        captions = {
            0: "讲师坐在桌前，屏幕显示标题：退款政策",
            30000: "画面出现文字：七天无理由退款",
            60000: "PPT 显示退款到账时间：3-5 个工作日",
            90000: "页面列出不支持退款的商品类别",
        }
        return [(t, captions.get(t, "")) for t, _ in frames]


class MockSceneDetector(SceneDetector):
    """模拟场景边界，故意制造两种极端来验证混合策略：
    - 5s 和 12s 两个短场景（应被合并，避免碎片）
    - 25s~90s 一个 65 秒长场景（超过 max 应被强制切开）
    """
    def detect(self, path: str) -> list[int]:
        return [5000, 12000, 25000]   # 边界毫秒；0 和末尾由策略层补


# ============ 主解析器：实现 Parser 协议 ============
class VideoParser:
    """实现 Parser 协议：parse(path) -> list[ParsedSection]。

    和 PyMuPDFParser/DoclingParser 接口完全一致，工厂换 backend 即可切换。
    业务代码（split_document）一行不用改——这就是可拔插架构的实证。
    """

    name = "video"

    def __init__(
        self,
        extractor=None,          # 三路提取器，默认 Mock（可注入真实实现）
        segment_seconds: int | None = None,
        caption_enabled: bool | None = None,
        strategy: SegmentationStrategy | None = None,  # 切分策略（可拔插）
        scene_detector: SceneDetector | None = None,   # 场景边界来源（可拔插）
    ):
        self.extractor = extractor or MockExtractor()
        self.segment_seconds = segment_seconds or settings.VIDEO_SEGMENT_SECONDS
        self.caption_enabled = (
            settings.VIDEO_CAPTION_ENABLED if caption_enabled is None else caption_enabled
        )
        # 策略默认从配置读取：fixed=固定窗口（兜底基线），scene=场景边界优先
        self.strategy_name = settings.VIDEO_SEGMENT_STRATEGY
        if strategy is not None:
            self.strategy = strategy
        elif self.strategy_name == "scene":
            self.strategy = SceneAwareSegmenter()
        else:
            self.strategy = FixedWindowSegmenter(self.segment_seconds)
        self.scene_detector = scene_detector or MockSceneDetector()

    def parse(self, path: str) -> list[ParsedSection]:
        suffix = Path(path).suffix.lower()
        if suffix not in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            raise ValueError(f"VideoParser 不支持的格式: {suffix}")

        # 三路提取（真实环境可并行，这里串行保证骨架清晰）
        frames = self.extractor.extract(path, settings.VIDEO_FRAME_FPS)
        transcript = self.extractor.transcribe(path)
        captions = self.extractor.describe(frames) if self.caption_enabled else []

        # 场景策略才需要边界；固定窗口策略不调用检测器，省掉检测成本
        candidates = (
            self.scene_detector.detect(path)
            if isinstance(self.strategy, SceneAwareSegmenter) else []
        )

        # 策略只负责"在哪切"，产出切点；解析层负责"每段填什么内容"
        points = self.strategy.segment(
            candidates=candidates,
            transcript=transcript,
            max_ms=settings.VIDEO_MAX_SEGMENT_MS,
            min_ms=settings.VIDEO_MIN_SEGMENT_MS,
        )
        return self._align(path, transcript, captions, points)

    def _align(
        self,
        path: str,
        transcript: list[tuple[int, int, str]],
        captions: list[tuple[int, str]],
        points: list[int],
    ) -> list[ParsedSection]:
        """把带时间戳的三路数据，按策略给出的切点聚合成 ParsedSection。

        注意职责边界：策略只决定"边界在哪"（points），这里只做"每段聚合什么"。
        换策略（固定窗口 -> 场景切分）本方法一行不用改——这就是解耦的价值。
        """
        if not transcript or len(points) < 2:
            return []

        sections: list[ParsedSection] = []
        for start_ms, end_ms in zip(points, points[1:]):
            # 台词按其起点落在哪个区间来归档（不丢不重）
            parts = [
                text for ts, _te, text in transcript
                if start_ms <= ts < end_ms
            ]
            # 画面描述同理，按帧时间点落入区间
            for cap_ms, cap_text in captions:
                if start_ms <= cap_ms < end_ms and cap_text:
                    parts.append(f"[画面] {cap_text}")
            sections.append(ParsedSection(
                text=" ".join(parts),
                source_type="video",
                metadata={
                    "filename": Path(path).name,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                },
            ))
        return sections

    # 保留旧方法名兼容已有测试/脚本：固定窗口 = 旧行为
    def _align_and_segment(self, path, transcript, captions):
        points = FixedWindowSegmenter(self.segment_seconds).segment(
            [], transcript, 0, 0
        )
        return self._align(path, transcript, captions, points)
