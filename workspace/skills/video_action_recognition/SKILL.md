# 视频动作识别技术指导

本技能为基于视频的人体动作识别算法提供技术规范与实现指导。
适用于需要从视频中检测和分类人体动作、且不依赖 LLM 或模型训练的场景。

## 技术路线总览

对于不需要训练的视频动作分类任务，推荐以下流水线：

```
视频获取 → 帧采样 → 人体姿态估计(预训练) → 关键点轨迹提取 → 运动学特征计算 → 规则分类器 → 输出标签
```

## 一、视频获取与预处理

### 1.1 从 YouTube 下载视频

使用 `yt-dlp`（`youtube-dl` 的活跃维护分支）：

```python
import subprocess, os

def download_video(url: str, output_dir: str = "videos") -> str:
    os.makedirs(output_dir, exist_ok=True)
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_template,
        url,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    video_id = url.split("v=")[-1].split("&")[0]
    return os.path.join(output_dir, f"{video_id}.mp4")
```

依赖：`pip install yt-dlp`

### 1.2 帧采样策略

- 建议采样率：每秒 5-10 帧（fps=5~10）即可满足动作识别需求
- 对于短视频（<60s），可使用均匀采样（如每隔 N 帧取 1 帧）
- 对于长视频，可先做运动检测，只对有运动的片段进行分析

```python
import cv2

def extract_frames(video_path: str, sample_fps: int = 5) -> list:
    cap = cv2.VideoCapture(video_path)
    original_fps = cap.get(cv2.CAP_PROP_FPS)
    frame_interval = max(1, int(original_fps / sample_fps))
    frames = []
    idx = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        if idx % frame_interval == 0:
            frames.append(frame)
        idx += 1
    cap.release()
    return frames
```

## 二、人体姿态估计（MediaPipe Pose）

### 2.1 基本用法

MediaPipe Pose 提供 33 个人体关键点，无需训练，直接推理：

```python
import mediapipe as mp
import numpy as np

mp_pose = mp.solutions.pose

def detect_pose_sequence(frames: list) -> list:
    pose = mp_pose.Pose(
        static_image_mode=False,
        model_complexity=1,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    keypoints_sequence = []
    for frame in frames:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = pose.process(rgb)
        if results.pose_landmarks:
            kps = np.array([
                [lm.x, lm.y, lm.z, lm.visibility]
                for lm in results.pose_landmarks.landmark
            ])
            keypoints_sequence.append(kps)
        else:
            keypoints_sequence.append(None)
    pose.close()
    return keypoints_sequence
```

依赖：`pip install mediapipe opencv-python`

### 2.2 关键点索引（常用）

| 索引 | 关键点 | 用途 |
|------|--------|------|
| 0    | NOSE   | 头部位置 |
| 11   | LEFT_SHOULDER | 肩膀（旋转检测） |
| 12   | RIGHT_SHOULDER | 肩膀（旋转检测） |
| 13   | LEFT_ELBOW | 手臂运动 |
| 14   | RIGHT_ELBOW | 手臂运动 |
| 15   | LEFT_WRIST | 手腕（拍打检测） |
| 16   | RIGHT_WRIST | 手腕（拍打检测） |
| 23   | LEFT_HIP | 髋部（旋转检测） |
| 24   | RIGHT_HIP | 髋部（旋转检测） |

## 三、动作分类规则

### 3.1 Arm Flapping（手臂拍打）

**特征**：双手腕或手肘在垂直方向（y 轴）呈现高频、重复的上下运动。

检测逻辑：
1. 提取左右手腕（15, 16）的 y 坐标时间序列
2. 计算 y 坐标的一阶差分，统计方向变化次数（零交叉数）
3. 若零交叉数 / 时间 > 阈值（如 2 次/秒），且手腕运动幅度 > 阈值，判定为 arm_flapping

### 3.2 Head Banging（头部撞击/摇晃）

**特征**：头部（鼻子关键点）在垂直或前后方向呈现重复的大幅运动，而躯干相对稳定。

检测逻辑：
1. 提取鼻子（0）的 y 坐标时间序列
2. 以双肩中点作为躯干参考，计算头部相对于躯干的运动幅度
3. 统计头部 y 坐标的零交叉频率
4. 若头部相对运动频率 > 阈值且躯干相对稳定，判定为 head_banging

### 3.3 Spinning（旋转）

**特征**：整个身体围绕垂直轴旋转，表现为肩膀线段方向角的持续单方向变化。

检测逻辑：
1. 提取左右肩（11, 12）的 x 坐标
2. 计算肩膀连线在水平面的方向角 θ = atan2(dy, dx)
3. 计算 θ 的累积变化量
4. 若累积角度变化超过一定阈值（如 >360° 表示至少旋转一圈），判定为 spinning
5. 辅助特征：人体检测框中心的水平位移较小（原地旋转）或较大（移动旋转）

### 3.4 综合分类函数

```python
def classify_action(keypoints_sequence: list, fps: int = 5) -> dict:
    # 过滤无效帧
    valid_kps = [kp for kp in keypoints_sequence if kp is not None]
    if len(valid_kps) < 10:
        return {"label": "unknown", "confidence": 0.0}

    scores = {
        "arm_flapping": compute_arm_flapping_score(valid_kps, fps),
        "head_banging": compute_head_banging_score(valid_kps, fps),
        "spinning": compute_spinning_score(valid_kps, fps),
    }
    best_label = max(scores, key=scores.get)
    return {"label": best_label, "confidence": scores[best_label]}
```

## 四、代码组织要求

生成的代码必须同时遵守「算法代码提交规范」Skill：
- `main_process(video_url: str) -> dict` 作为主入口
- 每个核心函数（下载、帧提取、姿态检测、分类）必须独立可调用
- 文件顶部列出依赖清单
- Google 风格 docstring

## 五、依赖清单

```
yt-dlp>=2024.1.0
opencv-python>=4.8.0
mediapipe>=0.10.0
numpy>=1.24.0
flask>=2.3.0
flask-restx>=1.1.0
```
