"""演示脚本：查看视频解析->切分->Chunk 的完整数据流，并对比切分策略。

用 Mock 数据跑通全流程，不需要装 ffmpeg/whisper/torch 等重依赖。

用法（项目根目录）：
    .venv/bin/python scripts/show_video.py
        -> 默认固定 30s 窗口切分

    .venv/bin/python scripts/show_video.py --strategy scene
        -> 场景边界优先 + 时长约束切分（短合并/长强切）

    .venv/bin/python scripts/show_video.py --compare
        -> 并排对比固定窗口 vs 场景策略的切点差异（面试演示用）
"""
import argparse
import sys
from pathlib import Path

# 直接运行子目录脚本时，把项目根注入 sys.path（之前踩过的坑）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.ingestion.loader_video import (
    VideoParser, MockExtractor, MockSceneDetector,
)
from app.ingestion.splitter import RecursiveSplitter, section_to_chunks
from app.ingestion.video_segmenter import FixedWindowSegmenter, SceneAwareSegmenter


def fmt_ms(ms: int) -> str:
    """毫秒转 mm:ss，方便看懂时间戳。"""
    total = ms // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


def render(title: str, parser: VideoParser, caption: bool) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")
    sections = parser.parse("data/demo.mp4")  # Mock 提取器不看文件是否真实存在
    print(f"[阶段1] 切出 {len(sections)} 个片段（切点："
          + ", ".join(fmt_ms(s.metadata["start_ms"]) for s in sections) + "）")
    for i, s in enumerate(sections, 1):
        start, end = s.metadata["start_ms"], s.metadata["end_ms"]
        print(f"    section {i}: [{fmt_ms(start)}-{fmt_ms(end)}]  {s.text}")

    splitter = RecursiveSplitter(chunk_size=150, overlap=20)
    chunks = []
    for s in sections:
        chunks.extend(section_to_chunks(s, splitter))
    print(f"[阶段2] 走同一个 section_to_chunks -> {len(chunks)} 个 Chunk")
    if chunks:
        m = chunks[0].meta
        print(f"    首块时间戳: {fmt_ms(m.start_ms)}-{fmt_ms(m.end_ms)}, source={m.source_type}")


def compare() -> None:
    """并排展示三种切法在同一段视频上的切点，直观看差异。"""
    transcript = MockExtractor().transcribe("demo.mp4")
    candidates = MockSceneDetector().detect("demo.mp4")

    print("场景检测给出的候选边界:", ", ".join(fmt_ms(b) for b in candidates))
    print(f"时长约束: min={settings.VIDEO_MIN_SEGMENT_MS // 1000}s, "
          f"max={settings.VIDEO_MAX_SEGMENT_MS // 1000}s\n")

    fixed = FixedWindowSegmenter(30).segment([], transcript, 0, 0)
    print(f"固定30s窗口 -> {len(fixed)-1} 段，切点: "
          + ", ".join(fmt_ms(p) for p in fixed))

    scene = SceneAwareSegmenter().segment(
        candidates, transcript,
        settings.VIDEO_MAX_SEGMENT_MS, settings.VIDEO_MIN_SEGMENT_MS,
    )
    print(f"场景+约束  -> {len(scene)-1} 段，切点: "
          + ", ".join(fmt_ms(p) for p in scene))
    print("\n观察：0:05 短场景被并入相邻片段（切点消失）；0:25-1:30 超长场景被强切。")


def main() -> None:
    cli = argparse.ArgumentParser(description="查看视频切分数据流")
    cli.add_argument("--strategy", choices=["fixed", "scene"], default=None,
                     help="切分策略：fixed 固定窗口 / scene 场景边界优先")
    cli.add_argument("--segment", type=int, default=30, help="固定窗口秒数（默认 30）")
    cli.add_argument("--caption", action="store_true", help="开启画面描述（模拟 VLM）")
    cli.add_argument("--compare", action="store_true", help="对比三种切法的切点")
    args = cli.parse_args()

    if args.compare:
        print("=" * 68 + "\n切分策略对比\n" + "=" * 68)
        compare()
        return

    strategy = args.strategy or settings.VIDEO_SEGMENT_STRATEGY
    label = "固定窗口" if strategy == "fixed" else "场景边界优先"
    parser = VideoParser(
        segment_seconds=args.segment,
        caption_enabled=args.caption,
        strategy=(
            FixedWindowSegmenter(args.segment) if strategy == "fixed"
            else SceneAwareSegmenter()
        ),
    )
    render(f"VideoParser 演示  策略={label}  窗口={args.segment}s", parser, args.caption)


if __name__ == "__main__":
    main()
