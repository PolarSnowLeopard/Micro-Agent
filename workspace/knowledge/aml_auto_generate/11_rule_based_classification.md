# 基于规则的动作分类方法

## 概述

基于规则的动作分类不需要训练数据，而是依靠领域知识和运动学特征的阈值判定来识别动作类型。这种方法特别适合：
- 动作类别数量有限（3-10 类）
- 动作之间有明显的运动学差异
- 没有足够的标注数据进行训练
- 需要可解释的分类结果

## 方法论

### 1. 特征工程

从姿态关键点的时间序列中提取以下运动学特征：

**位移特征：**
- 关键点在 x/y/z 方向的总位移
- 关键点的运动轨迹长度
- 关键点相对于参考点（如躯干中心）的位移

**频率特征：**
- 零交叉率（Zero Crossing Rate）：信号穿过均值的次数/时间
- 主频率：时间序列的傅里叶变换主峰对应的频率
- 周期性：自相关函数的峰值

**幅度特征：**
- 坐标的标准差
- 峰峰值（最大值-最小值）
- 均方根（RMS）值

**角度特征：**
- 关节角度的变化范围
- 身体轴线（肩膀连线、髋部连线）的方向角变化
- 累积角度变化（用于旋转检测）

### 2. 零交叉率计算

零交叉率是检测重复性运动（如拍打、摇头）的核心特征：

```python
import numpy as np

def zero_crossing_rate(signal: np.ndarray) -> float:
    """计算信号的零交叉率（去均值后）。"""
    centered = signal - np.mean(signal)
    sign_changes = np.diff(np.sign(centered))
    crossings = np.count_nonzero(sign_changes)
    duration = len(signal)
    return crossings / duration if duration > 0 else 0.0
```

### 3. 累积角度变化（旋转检测）

```python
def cumulative_angle_change(left_pts: np.ndarray, right_pts: np.ndarray) -> float:
    """计算两个关键点连线方向角的累积变化量（弧度）。"""
    dx = right_pts[:, 0] - left_pts[:, 0]
    dy = right_pts[:, 1] - left_pts[:, 1]
    angles = np.arctan2(dy, dx)
    diffs = np.diff(angles)
    # 处理角度跳变（-π ↔ π）
    diffs = np.where(diffs > np.pi, diffs - 2*np.pi, diffs)
    diffs = np.where(diffs < -np.pi, diffs + 2*np.pi, diffs)
    return float(np.abs(np.sum(diffs)))
```

### 4. 分类决策

基于多个特征的加权评分：

```python
def classify_by_rules(features: dict) -> tuple[str, float]:
    scores = {}

    # Arm Flapping: 手腕高频上下运动
    wrist_zcr = features.get("wrist_y_zcr", 0)
    wrist_amp = features.get("wrist_y_amplitude", 0)
    scores["arm_flapping"] = min(1.0, (wrist_zcr / 4.0) * 0.6 + (wrist_amp / 0.15) * 0.4)

    # Head Banging: 头部相对躯干的高频运动
    head_zcr = features.get("head_relative_y_zcr", 0)
    head_amp = features.get("head_relative_y_amplitude", 0)
    scores["head_banging"] = min(1.0, (head_zcr / 3.0) * 0.6 + (head_amp / 0.1) * 0.4)

    # Spinning: 肩膀方向角的累积变化
    cumul_angle = features.get("shoulder_cumulative_angle", 0)
    scores["spinning"] = min(1.0, cumul_angle / (2 * 3.14159))

    best = max(scores, key=scores.get)
    return best, scores[best]
```

## 阈值调优策略

由于不使用训练，阈值需要通过以下方式确定：
1. **经验值**：根据动作的物理特性设定初始值
2. **少量样本验证**：用少量已标注数据验证和微调阈值
3. **自适应阈值**：基于视频自身的统计特性（如关键点运动的中位数）动态调整

## 置信度计算

分类置信度反映当前视频与各类动作特征的匹配程度：
- 将各特征的得分归一化到 [0, 1]
- 使用加权平均计算每类的综合得分
- 最高得分对应的类别即为预测结果
- 若最高得分 < 0.3，可输出 "unknown"

## 常见陷阱

1. **帧率影响**：零交叉率等频率特征依赖采样率，需要归一化到实际时间
2. **坐标归一化**：MediaPipe 输出的坐标已归一化，但视频宽高比不同可能影响 x/y 的尺度
3. **遮挡处理**：关键点 visibility 低时应跳过或插值
4. **多人场景**：MediaPipe 默认单人检测，多人场景需要额外处理
5. **视角变化**：同一动作在不同视角下骨架投影不同，需要使用视角不变特征（如关节角度、相对运动）
