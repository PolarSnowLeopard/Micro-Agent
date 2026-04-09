# 轻量与高效部署大模型

## QLoRA
- **论文**：《QLoRA: Efficient Finetuning of Quantized LLMs》
- **核心**：量化至4位+低秩矩阵更新，4GB显存微调7B模型，性能损失小于5%。

## GPTQ
- **论文**：《GPTQ: Accurate Post-Training Quantization for Generative Pre-trained Transformers》
- **核心**：后量化至2/4/8位，7B模型显存3GB，推理速度提升2倍。

## LoRA
- **论文**：《LoRA: Low-Rank Adaptation of Large Language Models》
- **核心**：低秩适应微调标准技术，仅训练少量参数，显存需求降低80%。

## vLLM
- **论文**：《vLLM: Easy, Fast, and Cheap LLM Serving with PagedAttention》
- **核心**：分页注意力机制，推理吞吐量提升10-100倍，部署成本降低90%。

## DistilBERT
- **论文**：《DistilBERT: A Distilled Version of BERT》
- **核心**：知识蒸馏，参数减少40%，速度提升60%，保持95%性能。

## MobileBERT
- **论文**：《MobileBERT: a Compact Task-Agnostic BERT》
- **核心**：面向移动设备，参数仅BERT的4%，速度提升5倍，手机端实时运行。

## AWQ
- **论文**：《AWQ: Activation-aware Weight Quantization》
- **核心**：激活感知权重量化，4位后性能优于GPTQ，速度再提升15%。

## FastChat
- **论文**：《FastChat: An Open Platform for Training, Serving, and Evaluating LLMs》
- **核心**：一站式训练部署平台，分布式推理，降低开源LLM落地成本。

## BitNet
- **论文**：《BitNet: Scaling 1-bit Transformers for LLMs》
- **核心**：1位权重量化，参数仅传统1/8，推理速度提升8倍，适合端侧部署。

## EdgeLLM
- **论文**：《EdgeLLM: Optimizing LLMs for Edge Devices》
- **核心**：量化+蒸馏+剪枝联合优化，2GB显存部署，延迟低于100ms。
