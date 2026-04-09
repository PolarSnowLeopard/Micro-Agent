# 训练与优化技术

## 缩放定律
- **论文**：《Scaling Laws for Neural Language Models》
- **核心**：揭示性能与参数规模、数据量、计算预算的关系，指导最优训练配置。

## Chinchilla定律
- **论文**：《Chinchilla Scaling Laws: Training Compute-Optimal LLMs》
- **核心**：固定预算下更小模型+更多数据更优，67B超越175B GPT-3，成本降低50%。

## Megatron-LM
- **论文**：《Megatron-LM: Training Multi-Billion Parameter LMs Using Model Parallelism》
- **核心**：张量并行+流水线并行+数据并行，65B训练速度提升3倍。

## DeepSpeed
- **论文**：《DeepSpeed: System Optimizations for Trillion-Parameter Models》
- **核心**：ZeRO内存优化、混合精度训练、推理加速。显存需求降低90%。

## FlashAttention
- **论文**：《FlashAttention: Fast and Memory-Efficient Exact Attention》
- **核心**：IO感知高效注意力，速度提升2-4倍，显存降低50%，现代LLM核心优化。

## Mixtral MoE
- **论文**：《Mixtral of Experts: A Sparse Mixture of Experts Language Model》
- **模型**：Mistral AI Mixtral-8x7B
- **核心**：8个7B专家，每token仅激活2个。56B参数推理成本仅14B。

## RAM高效训练
- **论文**：《Training Large Language Models with Random Access Memory》
- **核心**：激活检查点+梯度checkpoint，单卡可训练13B模型。

## 数据高效微调
- **论文**：《Data-Efficient Fine-Tuning for Large Language Models》
- **核心**：少样本提示+数据筛选+增量微调，1000条样本接近全量效果。

## 自动数据清洗
- **论文**：《Automatic Data Curation for Large Language Model Pre-Training》
- **核心**：质量评分+去重+有害内容过滤，数据质量提升40%，模型性能提升15%。

## 分布式训练
- **论文**：《Distributed Training of Large Language Models: A Survey》
- **核心**：模型并行+数据并行+混合并行策略，分布式配置模板与优化指南。
