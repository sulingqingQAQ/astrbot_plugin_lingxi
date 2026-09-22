"""群活跃度评分模块（移植自 chatluna-character 的 utils/activity.ts）。

纯数学，**零 LLM 调用**。用多时间窗的消息速率经 logistic 软阈值折算成 0~1 的
"此刻群里多热闹"分数，再叠加"我上一次开口是什么时候"的自罚，得到一个自然
的软冷却：

- 群越活跃 → 分数越高 → 越值得让机器人插话
- 机器人刚说过话 → 分数被压低 → 紧接着再开口的概率自然下降

分数本身不决定"说什么"，只用来做**粗筛**：分数太低时不值得为这个群花一次
判定模型的调用。真正的语义判断（该不该说、从什么角度说）仍交给 LLM。

设计来源：koishi-plugin-chatluna-character 0.0.230
- ``src/utils/activity.ts``   评分函数与全部常量
- ``src/plugins/filter.ts``   markTriggered 的自罚逻辑（COOLDOWN_PENALTY）

原版用毫秒时间戳，本移植统一改为**秒**（Python ``time.time()`` 的天然单位），
所有时间窗口常量已按秒折算，速率的单位统一为"条/分钟"，与阈值量级保持一致。
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------- #
# 时间窗口（秒）
# ---------------------------------------------------------------------- #

# 消息时间戳环形缓冲的容量（条）
WINDOW_SIZE = 90
# 持续速率观测窗：判断这个群"这段时间一直在聊"
RECENT_WINDOW = 90.0
# 瞬时速率观测窗：判断"最近这几十秒聊得凶"
INSTANT_WINDOW = 20.0
# 爆发速率观测窗：短时间内的连续刷屏
SHORT_BURST_WINDOW = 30.0
# 指数平滑窗：让分数变化平滑，避免一两条消息就跳变
SMOOTHING_WINDOW = 8.0
# 新鲜度半衰期：最后一条消息越久远，分数衰减越多
FRESHNESS_HALF_LIFE = 60.0
# 发言后的硬压冷却：这几秒内分数被平方压制
MIN_COOLDOWN_TIME = 6.0

# ---------------------------------------------------------------------- #
# 速率阈值（条/分钟）与 softmax 缩放
# ---------------------------------------------------------------------- #

# 持续速率低于这个量级视作"群很安静"
SUSTAINED_RATE_THRESHOLD = 10.0
SUSTAINED_RATE_SCALE = 3.0
# 瞬时速率阈值
INSTANT_RATE_THRESHOLD = 9.0
INSTANT_RATE_SCALE = 2.0
# 爆发速率阈值与加权
BURST_RATE_THRESHOLD = 12.0
BURST_RATE_SCALE = 4.0

# 持续分量与瞬时分量的权重（原版 0.65 / 0.35）
SUSTAINED_WEIGHT = 0.65
INSTANT_WEIGHT = 0.35

# 观测量不足时的下限：少于这么多条消息直接认为不活跃
MIN_RECENT_MESSAGES = 6

# ---------------------------------------------------------------------- #
# 自罚与阈值自适应
# ---------------------------------------------------------------------- #

# 每说一句，自己的分数扣掉这么多（原版 COOLDOWN_PENALTY）。
# 注意：单靠扣分不够——分数会随群重新活跃而按指数平滑回升（约 10 秒就回到高位），
# 所以必须配合下面的"抬高门槛"一起用，两者才是完整的抑制机制。
COOLDOWN_PENALTY = 0.8

# 群安静这么久之后，被抬高的门槛归位（原版 THRESHOLD_RESET_TIME = 10 分钟）。
# 语义是"这事儿翻篇了"：隔了足够久，重新从最宽松的门槛开始。
THRESHOLD_RESET_TIME = 600.0

# 每开口一次，门槛往上抬的步长 = 阈值区间 × 此比例（原版 0.1）。
# 说得越勤，门槛越高，直到顶到上限为止。
THRESHOLD_STEP_RATIO = 0.1


def logistic(value: float) -> float:
    """数值稳定的 logistic 函数，把任意实数压到 (0, 1)。"""
    if not math.isfinite(value):
        return 0.0
    # 与 JS 原版保持一致：超出 ±10 直接夹住，避免 exp 溢出
    if value > 10.0:
        return 0.99995
    if value < -10.0:
        return 0.00005
    return 1.0 / (1.0 + math.exp(-value))


def clamp(value: float, low: float, high: float) -> float:
    """把 value 夹在 [low, high] 内。"""
    return max(low, min(value, high))


def calculate_freshness_factor(timestamps: list[float], now: float) -> float:
    """新鲜度因子：最后一条消息距今越远，返回值越小（指数衰减）。"""
    if not timestamps:
        return 0.0
    return math.exp(-(now - timestamps[-1]) / FRESHNESS_HALF_LIFE)


def smooth_score(
    target: float,
    previous: float,
    previous_timestamp: float,
    now: float,
) -> float:
    """指数平滑：让分数从 previous 缓动到 target，而不是一步跳过去。

    平滑系数取决于距上次更新的时长——间隔越久，一次可以走越多。
    """
    if not previous_timestamp or previous_timestamp <= 0:
        return target

    elapsed = now - previous_timestamp
    if elapsed <= 0:
        return target

    factor = 1.0 - math.exp(-elapsed / SMOOTHING_WINDOW)
    return previous + (target - previous) * clamp(factor, 0.0, 1.0)


def calculate_activity_score(
    timestamps: list[float],
    last_response_time: float,
    max_messages: int,
    previous_score: float,
    previous_timestamp: float,
    now: float,
) -> tuple[float, float]:
    """算出当前群的活跃度分数。

    参数
    ----
    timestamps
        群消息到达时间（秒，升序）。超过 ``WINDOW_SIZE`` 条的旧记录应已在调用方裁剪。
    last_response_time
        机器人上一次开口的时间（秒）；``0`` 表示本次启动后还没说过话。
    max_messages
        群消息量上限，用于"快溢出时略微加分"。传 ``0`` 表示不启用这项。
    previous_score / previous_timestamp
        上一次的分数与其计算时刻，用于指数平滑。
    now
        当前时间（秒）。

    返回
    ----
    ``(分数, 计算时刻)``，分数已夹在 [0, 1]。
    """
    # 样本太少：视为安静，但仍走平滑，避免分数断崖
    if len(timestamps) < 2:
        return smooth_score(0.0, previous_score, previous_timestamp, now), now

    recent = [ts for ts in timestamps if now - ts <= RECENT_WINDOW]
    if len(recent) < MIN_RECENT_MESSAGES:
        return smooth_score(0.0, previous_score, previous_timestamp, now), now

    # 窗口内条数换算成"条/分钟"
    sustained_rate = (len(recent) / RECENT_WINDOW) * 60.0

    instant = [ts for ts in timestamps if now - ts <= INSTANT_WINDOW]
    instant_rate = (len(instant) / INSTANT_WINDOW) * 60.0

    burst = [ts for ts in timestamps if now - ts <= SHORT_BURST_WINDOW]
    burst_rate = (len(burst) / SHORT_BURST_WINDOW) * 60.0

    sustained_component = logistic(
        (sustained_rate - SUSTAINED_RATE_THRESHOLD) / SUSTAINED_RATE_SCALE
    )
    instant_component = logistic(
        (instant_rate - INSTANT_RATE_THRESHOLD) / INSTANT_RATE_SCALE
    )

    combined = (
        sustained_component * SUSTAINED_WEIGHT + instant_component * INSTANT_WEIGHT
    )

    # 短时间连续刷屏：额外加权
    if burst_rate > BURST_RATE_THRESHOLD:
        burst_contribution = clamp(
            (burst_rate - BURST_RATE_THRESHOLD) / BURST_RATE_SCALE, 0.0, 1.0
        )
        combined += burst_contribution * 0.25

    # 消息间隔规律性：大家你来我往地聊（间隔小），比零星冒泡更值得参与
    if len(instant) >= 6:
        start_index = max(len(timestamps) - len(instant), 0)
        relevant = timestamps[start_index:]
        intervals = [
            relevant[i] - relevant[i - 1] for i in range(1, len(relevant))
        ]
        if intervals:
            average_gap = sum(intervals) / len(intervals)
            interval_component = logistic((12.0 - average_gap) / 6.0)
            combined *= 0.7 + 0.3 * interval_component

    # 新鲜度：最后一条消息过去太久，说明话题已经凉了
    freshness = calculate_freshness_factor(timestamps, now)
    combined *= 0.55 + 0.45 * freshness

    # 消息量逼近上限时略微加分（原版用于"快聊满了"的场景）
    if max_messages and len(recent) >= max_messages * 0.9:
        combined += 0.08

    # 刚开口过：平方压制，越近压得越狠
    if last_response_time:
        since_reply = now - last_response_time
        if since_reply < MIN_COOLDOWN_TIME:
            cooldown_ratio = since_reply / MIN_COOLDOWN_TIME
            combined *= cooldown_ratio * cooldown_ratio

    smoothed = smooth_score(
        clamp(combined, 0.0, 1.0), previous_score, previous_timestamp, now
    )
    return clamp(smoothed, 0.0, 1.0), now
