# 基础大语言模型（LLM）

## LLaMA
- **论文**：《LLaMA: Open and Efficient Foundation Language Models》
- **模型**：Meta LLaMA 1（7B/13B/33B/65B）
- **核心**：仅用1.4T公开tokens训练，13B性能优于175B GPT-3。采用RMSNorm预归一化、SwiGLU激活函数、RoPE位置编码，通过xformers高效注意力优化。

## LLaMA 2
- **论文**：《LLaMA 2: Open Foundation and Fine-Tuned Chat Models》
- **模型**：Meta LLaMA 2（7B/13B/70B）
- **核心**：扩展数据量，引入RLHF优化对话能力。70B模型接近闭源模型表现，商用许可友好。

## Mistral 7B
- **论文**：《Mistral 7B: A Fast and Efficient Language Model》
- **模型**：Mistral AI Mistral-7B
- **核心**：滑动窗口注意力（SWA）降低复杂度，推理速度比同类快50%。6.5GB显存即可部署。

## InstructGPT
- **论文**：《Training language models to follow instructions with human feedback》
- **核心**：提出RLHF范式，通过人类评分训练奖励模型，用PPO算法优化语言模型，为ChatGPT奠定技术基础。

## Transformer
- **论文**：《Attention Is All You Need》
- **核心**：自注意力机制实现并行计算。多头注意力、位置编码等核心组件成为所有现代大模型的架构基础。

## T5
- **论文**：《Exploring the Limits of Transfer Learning with a Unified Text-to-Text Transformer》
- **模型**：Google T5（11B等多规模）
- **核心**：文本到文本统一框架，将所有NLP任务转化为文本生成任务。

## OPT-175B
- **论文**：《OPT: Open Pre-trained Transformer Language Models》
- **模型**：Meta OPT-175B
- **核心**：首个开源175B参数大模型，对标GPT-3，提供完整训练与推理代码。

## Gemma
- **论文**：《Gemma: Open Models Based on Gemini Technology》
- **模型**：Google Gemma-7B
- **核心**：基于Gemini架构简化，主打合规性与安全性，适配教育、企业等场景。

## ChatGLM
- **论文**：《ChatGLM: Efficient Tuning of Generalized Language Models for Chatbots》
- **模型**：清华&智谱AI ChatGLM-6B / ChatGLM3
- **核心**：中文优化分词与词向量，支持INT4量化单卡部署。ChatGLM3支持128K长上下文与工具调用。

## Qwen
- **论文**：《Qwen: A Comprehensive Study of Large Language Models》
- **模型**：阿里Qwen1.5-7B / Qwen2-7B
- **核心**：中文能力天花板级，动态自适应位置编码支持128K上下文，中文增强自注意力准确率提升8%-12%。

## Vicuna
- **论文**：《Vicuna: An Open-Source Chatbot Impressing GPT-4》
- **模型**：UC伯克利 Vicuna-13B
- **核心**：基于LLaMA微调，90%回答质量接近ChatGPT。

## Falcon
- **论文**：《Falcon: Optimized Foundation Models for Industrial Use》
- **模型**：Falcon-40B
- **核心**：面向工业场景，多语言表现优异，开源许可友好。

## PaLM
- **论文**：《PaLM: Scaling Language Modeling with Pathways》
- **模型**：Google PaLM 540B
- **核心**：Pathways并行框架，少样本学习突破，支持100+语言。
