# 多模态大模型

## Stable Diffusion
- **论文**：《High-Resolution Image Synthesis with Latent Diffusion Models》
- **模型**：Stability AI Stable Diffusion 1.5/2.0
- **核心**：潜在扩散模型（LDM），图像压缩到潜在空间扩散，大幅降低计算成本。支持文生图、图像修复。

## GPT-4V
- **论文**：《GPT-4V(ision): Capabilities, Limitations, and Societal Impact》
- **核心**：大语言模型与视觉深度融合，支持图像理解、图文问答、图像描述。

## Gemini
- **论文**：《Gemini: A Family of Multimodal Large Language Models》
- **模型**：Google Gemini Pro
- **核心**：原生多模态，直接融合文本、图像、音频。支持跨模态推理。

## Flamingo
- **论文**：《Flamingo: a Visual Language Model for Few-Shot Learning》
- **核心**：视觉编码器+语言模型+跨模态注意力架构，少样本视觉学习。

## BLIP-2
- **论文**：《BLIP-2: Bootstrapping Language-Image Pre-training》
- **模型**：Salesforce BLIP-2
- **核心**：冻结图像编码器+可训练桥接模块+冻结LLM，训练成本仅为同类1/10。

## MiniGPT-4
- **论文**：《MiniGPT-4: Enhancing Vision-Language Understanding》
- **核心**：轻量级多模态，连接CLIP和LLaMA，少量数据微调即可实现类GPT-4V基础能力。

## LLaVA
- **论文**：《LLaVA: Large Language and Vision Assistant》
- **模型**：LLaVA-1.5
- **核心**：基于CLIP和LLaMA的开源多模态助手，支持中文图文对话。

## AudioLM
- **论文**：《AudioLM: a Language Modeling Approach to Audio Generation》
- **核心**：语言建模思想应用于音频生成，支持音频续写、风格迁移。

## Whisper
- **论文**：《Whisper: Robust Speech Recognition via Large-Scale Supervised Training》
- **模型**：OpenAI Whisper（tiny/base/small等）
- **核心**：支持100+语言语音转文字，Encoder-Decoder架构，tiny版可在手机端部署。

## CoOp
- **论文**：《CoOp: Conditional Prompt Learning for Vision-Language Models》
- **核心**：条件式提示词生成适配视觉分类任务，无需修改模型权重。
