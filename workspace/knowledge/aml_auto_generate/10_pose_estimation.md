# 人体姿态估计技术

## 概述

人体姿态估计（Human Pose Estimation）的目标是从图像或视频中检测人体关键点（关节、头部等）的位置。这些关键点构成人体骨架，是动作识别的基础特征。

## 主流预训练姿态估计方案

### MediaPipe Pose（推荐）

Google 开发的轻量级实时姿态估计解决方案。

优势：
- 无需 GPU，CPU 即可实时运行
- 33 个全身关键点（包括面部、手部关键点）
- 内置人体检测，无需额外的检测器
- Python API 简洁易用
- 活跃维护，跨平台支持

安装：`pip install mediapipe`

关键参数：
- `model_complexity`：0（快速）/ 1（平衡）/ 2（精确）
- `min_detection_confidence`：初始检测阈值
- `min_tracking_confidence`：跟踪阈值
- `static_image_mode`：True 表示独立图像，False 表示视频序列（启用跟踪优化）

33 个关键点说明：
- 0: NOSE（鼻子）
- 1-10: 面部关键点（眼睛、耳朵、嘴角）
- 11: LEFT_SHOULDER, 12: RIGHT_SHOULDER
- 13: LEFT_ELBOW, 14: RIGHT_ELBOW
- 15: LEFT_WRIST, 16: RIGHT_WRIST
- 17-22: 手部关键点（拇指、食指、小指）
- 23: LEFT_HIP, 24: RIGHT_HIP
- 25: LEFT_KNEE, 26: RIGHT_KNEE
- 27: LEFT_ANKLE, 28: RIGHT_ANKLE
- 29-32: 脚部关键点

每个关键点输出：x, y（归一化到 [0,1]）, z（深度，相对于髋部）, visibility（可见性置信度）

### OpenPose

CMU 开发的多人姿态估计系统。

特点：
- 支持多人检测（Bottom-Up 方法）
- 25 个身体关键点 + 手部 + 面部
- 精度较高但速度较慢，通常需要 GPU
- 安装较复杂

适用场景：多人场景、需要更高精度时

### MMPose

OpenMMLab 的姿态估计工具箱。

特点：
- 提供大量预训练模型（HRNet、ViTPose 等）
- 支持 Top-Down 和 Bottom-Up 方法
- 需要 PyTorch + mmcv 环境
- 适合研究和高精度需求

## 关键点坐标系

MediaPipe 使用归一化坐标：
- x: 水平方向，0（左）到 1（右）
- y: 垂直方向，0（上）到 1（下）
- z: 深度方向，以髋部为参考，负值表示靠近相机

注意：归一化坐标与图像实际分辨率无关，便于跨分辨率比较。

## 关键点质量评估

在实际应用中，需要处理姿态估计失败的情况：
- `visibility` < 0.5 的关键点可能不可靠
- 连续多帧检测失败（返回 None）可能是人物被遮挡或离开画面
- 建议对关键点坐标做平滑处理（如移动平均或卡尔曼滤波）减少抖动

## 从关键点到运动学特征

### 关节角度

通过三个关键点计算关节角度：
```
angle = atan2(|cross_product|, dot_product)
```
常用关节：肘关节（肩-肘-腕）、膝关节（髋-膝-踝）

### 运动频率

对关键点坐标的时间序列做傅里叶变换或零交叉分析，提取主频率。
- 高频上下运动 → 拍打类动作
- 低频前后运动 → 摇头/点头
- 持续角度变化 → 旋转

### 运动幅度

关键点坐标的标准差或峰峰值反映运动幅度：
- 手腕 y 坐标标准差大 → 手臂运动剧烈
- 头部相对躯干的 y 偏移变化大 → 头部运动
