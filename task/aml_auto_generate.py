"""算法模型想定式开发 — prompt 构建。

v2 架构版本：
- 代码规范通过 Skill 注入（algorithm_code_standards）
- 知识库通过 EmbeddingRetriever 在 Agent.run() 中自动检索
- prompt 只关注任务本身
"""

from __future__ import annotations


def build_aml_auto_generate_prompt(
    *,
    model_name: str,
    free_narrative: str,
    workspace: str,
    industry: str = "",
    scenario: str = "",
    technology: str = "",
    paper_content: str = "",
) -> str:
    industry_section = f"\n## 行业领域\n{industry}" if industry else ""
    scenario_section = f"\n## 应用场景\n{scenario}" if scenario else ""
    technology_section = f"\n## 技术方向\n{technology}" if technology else ""
    paper_section = (
        f"\n## 想定式描述文件内容\n{paper_content[:3000]}"
        if paper_content else ""
    )

    return f"""你是一个专业的AI算法工程师，需要根据用户的需求生成高质量的算法模型服务代码。

## 任务目标
生成算法模型：**{model_name}**

## 用户需求描述
{free_narrative}
{industry_section}{scenario_section}{technology_section}{paper_section}

---

## 执行步骤（严格按步骤执行）

### 步骤 1：分析需求并设计架构
阅读用户需求与参考资料（如果 RAG 检索到了相关论文/模型），设计：
- 哪些函数作为独立、可封装的核心 API
- `main_process` 主入口的职责边界
- Flask 路由如何仅转发到核心函数

### 步骤 2：生成算法代码
生成完整 Python 单文件代码，必须严格遵守已加载的「算法代码提交规范」Skill 中的所有要求。

### 步骤 3：保存代码文件
使用 bash 工具将代码写入 `{workspace}/temp/{model_name}_algorithm.py`。

### 步骤 4：代码质量自检（七维）
对生成的代码逐项分析：
1. 功能测试  2. 平台提交规范  3. 接口测试  4. 性能测试  5. 可靠性测试  6. 安全性测试  7. 兼容性测试

### 步骤 5：保存最终结果
使用 bash 工具将 JSON 写入 `{workspace}/temp/aml_generate_result.json`，格式：
```json
{{{{
    "model_name": "{model_name}",
    "generated_code": "<完整代码>",
    "code_filename": "{model_name}_algorithm.py",
    "test_results": [
        {{{{"name": "功能测试", "status": "passed", "description": "...", "details": "..."}}}}
    ],
    "references": [
        {{{{"type": "paper", "title": "...", "summary": "..."}}}}
    ]
}}}}
```

### 步骤 6：完成任务
确认 JSON 文件已保存后，调用 terminate 结束任务。

---

## 注意事项
1. 平台规范优先：严格遵守 Skill 中的函数独立性与 Google docstring 要求
2. 代码完整性：不要用省略号或 pass 代替实现
3. 如果 RAG 检索到了参考研究，在 references 字段中列出
4. 逐步执行，不要跳过任何步骤

现在开始执行任务，请从【步骤 1】开始。
"""
