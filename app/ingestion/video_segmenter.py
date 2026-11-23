"""视频切分策略：可拔插的片段边界生成器。

为什么单独抽一层？
    上一版 VideoParser._align_and_segment 把"按固定时间窗口切"写死在解析器里。
    按可拔插架构的原则，"怎么切片段"是会演进的策略（固定窗口 -> 场景切分 ->
    音轨话题融合），应该和"怎么提取三路数据"解耦。所以抽成 SegmentationStrategy
    协议，VideoParser 只依赖协议，具体策略由工厂/配置注入。

两种策略：
    FixedWindowSegmenter  按固定时间窗口切（等价于文档的固定长度硬切，兜底基线）
    SceneAwareSegmenter   场景边界优先 + 时长约束（短合并/长强切），等价于文档的
                          递归切分：强语义边界优先，超长降级到固定窗口兜底

核心概念（面试要点）：
    场景边界（scene boundary）：画面语义发生转折的时间点，来自镜头边界检测
        （Shot Boundary Detection, SBD）。硬切靠帧间像素/色彩突变，渐变转场靠
        亮度阈值或深度学习（TransNetV2）。
    场景边界 != 话题边界：PPT 翻页触发场景切换但话题连续；固定镜头换话题画面不变。
        所以本模块把"边界"抽象成可注入的候选点列表，画面检测和音轨话题检测
        产出的边界都能进来融合，而不是绑死在画面上。
"""
from math import ceil
from typing import Protocol, runtime_checkable


@runtime_checkable
class SceneDetector(Protocol):
    """场景边界检测：视频 -> 边界时间戳列表（毫秒）。

    实现可以是 PySceneDetect(ContentDetector/ThresholdDetector)、TransNetV2，
    或基于音轨话题变化的检测器。VideoParser 不关心边界怎么来的。
    """

    def detect(self, path: str) -> list[int]:
        """返回场景切换的时间点（毫秒），不含 0 和视频总时长。"""
        ...


@runtime_checkable
class SegmentationStrategy(Protocol):
    """切分策略：给定候选边界，产出最终的片段边界列表。

    输入是"候选边界"而不是视频文件——这样策略层可以脱离重依赖单独测试。
    输出是 [start_ms, end_ms, ...] 这样的切点序列（含首尾）。
    """

    def segment(
        self,
        candidates: list[int],
        transcript: list[tuple[int, int, str]],
        max_ms: int,
        min_ms: int,
    ) -> list[int]:
        """协议方法：由各实现提供具体切点算法。"""
        ...


def _video_end_ms(transcript: list[tuple[int, int, str]], fallback_ms: int = 0) -> int:
    """从转录文本推断视频时长（最后一句的 end）。骨架阶段没有真实时长字段，
    所以用转录末尾兜底；真实接入时可传入容器 metadata 的 duration。"""
    return max((end for _, end, _ in transcript), default=fallback_ms)


class FixedWindowSegmenter:
    """固定时间窗口切分：按 window_seconds 等分时间轴。

    优点：简单、可预测。缺点：会切断完整场景，也可能把两个话题塞进一个片段。
    它是场景策略不可用时的兜底基线。
    """

    def __init__(self, window_seconds: int = 30):
        self.window_ms = window_seconds * 1000

    def segment(
        self,
        candidates: list[int],
        transcript: list[tuple[int, int, str]],
        max_ms: int = 0,
        min_ms: int = 0,
    ) -> list[int]:
        """按 window_ms 等分时间轴，忽略候选边界与时长约束参数。"""
        end_ms = _video_end_ms(transcript)
        if end_ms <= 0:
            return []
        # range 到 end_ms+window，保证最后一段的终点正好是视频末尾附近
        points = list(range(0, end_ms + 1, self.window_ms))
        if points[-1] < end_ms:
            points.append(end_ms)
        return points


class SceneAwareSegmenter:
    """场景边界优先 + 时长约束的切分策略。

    算法四步（和文档递归切分同构：强边界优先，弱边界兜底）：
      1. 把候选边界 + 首尾(0, 视频末尾)合并、排序、去重
      2. 短片段合并：短于 min_ms 的片段并入相邻片段（避免 1~2 秒语义碎片）
      3. 长片段强切：长于 max_ms 的片段均分成 N 段（降级到时间边界兜底）
      4. 再跑一次短合并，处理强切产生的小尾巴

    用 ceil 均分而不是按 max 步长切，是为了避免最后出现一个很短的残段。
    """

    def __init__(self, detector: SceneDetector | None = None):
        self.detector = detector  # 边界从哪来由外部决定，策略只消费边界

    def segment(
        self,
        candidates: list[int],
        transcript: list[tuple[int, int, str]],
        max_ms: int = 60000,
        min_ms: int = 10000,
    ) -> list[int]:
        """场景边界优先，经短合并/长强切后返回合法切点列表。"""
        end_ms = _video_end_ms(transcript)
        if end_ms <= 0:
            return []

        # 1. 候选边界 + 首尾，排序去重。只保留 (0, end_ms) 范围内的点。
        points = sorted({0, end_ms} | {b for b in candidates if 0 < b < end_ms})
        points = self._merge_short(points, min_ms)
        points = self._split_long(points, max_ms)
        points = self._merge_short(points, min_ms)  # 4. 处理强切产生的小尾巴
        return points

    @staticmethod
    def _merge_short(points: list[int], min_ms: int) -> list[int]:
        """把短于 min_ms 的片段并入相邻片段。

        合并方向有讲究：末尾的短段要往前并（删起点），其余往后并（删终点），
        因为首尾两个点是锚点不能删除，否则会丢掉视频头尾。
        """
        points = list(points)
        merged = True
        while merged and len(points) > 2:
            merged = False
            for i in range(len(points) - 1):
                if points[i + 1] - points[i] < min_ms:
                    if i + 1 == len(points) - 1:
                        del points[i]        # 末段太短：删起点，并到前一段
                    else:
                        del points[i + 1]    # 其余：删终点，并到后一段
                    merged = True
                    break
        return points

    @staticmethod
    def _split_long(points: list[int], max_ms: int) -> list[int]:
        """把长于 max_ms 的片段均分成 N 段，N = ceil(段长 / max_ms)。"""
        result = list(points)
        i = 0
        while i < len(result) - 1:
            length = result[i + 1] - result[i]
            if length > max_ms:
                n = ceil(length / max_ms)
                step = length / n
                inserts = [int(result[i] + step * k) for k in range(1, n)]
                result[i + 1 : i + 1] = inserts
                i += len(inserts)  # 跳过刚插入的点，避免死循环
            i += 1
        return result
