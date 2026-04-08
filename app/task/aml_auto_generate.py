from app.config import WORKSPACE_ROOT


def get_aml_auto_generate_prompt(
    model_name: str,
    free_narrative: str,
    industry: str = "",
    scenario: str = "",
    technology: str = "",
    paper_content: str = "",
    knowledge_context: str = "",
) -> str:
    """
    生成「算法模型想定式开发」的 Agent 提示词

    参数:
        model_name: 算法模型名称
        free_narrative: 用户自由叙述的需求
        industry: 行业（可选）
        scenario: 场景（可选）
        technology: 技术方向（可选）
        paper_content: 想定式描述文件提取的文本内容（可选）
        knowledge_context: 知识库检索结果（可选）

    返回:
        用于 Agent 的提示词字符串
    """
    industry_section = f"\n## 行业领域\n{industry}" if industry else ""
    scenario_section = f"\n## 应用场景\n{scenario}" if scenario else ""
    technology_section = f"\n## 技术方向\n{technology}" if technology else ""
    paper_section = (
        f"\n## 想定式描述文件内容\n{paper_content[:3000]}"
        if paper_content
        else ""
    )
    knowledge_section = (
        f"\n## 参考的相关研究与模型\n{knowledge_context}"
        if knowledge_context
        else ""
    )

    prompt = f"""你是一个专业的AI算法工程师，需要根据用户的需求生成高质量的算法模型服务代码。

## 任务目标
生成算法模型：**{model_name}**

生成的单文件代码必须与平台《算法代码提交要求》（与「垂域原子微服务发布」中算法提交规范一致）对齐，便于后续上传同一平台进行**自动代码分析**与**封装为 MCP 服务**。

## 用户需求描述
{free_narrative}
{industry_section}{scenario_section}{technology_section}{paper_section}{knowledge_section}

---

## 平台算法代码提交规范（必须严格遵守）

以下规范与平台文档 `算法代码提交要求` 一致，违反将导致后续 MCP 封装失败或分析质量下降。

### 1. 核心功能函数必须完全独立（最重要）
- 每个**可被封装为 MCP 工具**的核心函数必须能**单独直接调用**，不依赖模块级/全局可变状态（例如：禁止在函数外 `model = load_model()` 再在函数内使用）。
- 模型加载、配置读取、资源路径解析等**必须在函数内部完成**；若需缓存，使用函数内局部闭包或显式传入已加载对象，但**推荐**每次调用在函数内完成加载以保证独立性与可测试性（简单场景可接受函数内单例懒加载，但须在 docstring 中说明）。
- **错误示例**：全局变量持有模型，函数只调用 `global model`。
- **正确示例**：`def run_inference(data_path: str, model_path: Optional[str] = None) -> str:` 内在函数体中加载模型并推理。

### 2. 类型注解与 Google 风格文档字符串（必须）
- 所有**对外核心函数**（含 `main_process` 及业务入口函数）必须具备**完整类型注解**（参数与返回值）。
- 每个核心函数必须使用 **Google 风格 docstring**，至少包含：
  - 第一行：简短功能描述（可用中文）
  - `Args:` 下列出每个参数
  - `Returns:` 下列出返回值含义
- 模块顶部可使用常规中文注释说明业务背景；**函数级说明以 Google docstring 为准**。

### 3. 代码结构：算法核心 + 薄层 Web（推荐形态）
- **算法层**：在文件中定义清晰的顶层函数（命名建议包含业务语义，且必须包含一个 **`main_process(...)`** 或等价的**单一主入口函数**，作为「项目核心入口」，签名需带完整类型注解）。
- **Web 层（可选但推荐）**：若提供 HTTP 服务，使用 **Flask + flask-restx** 生成 Swagger；**路由处理函数只做参数校验与调用上述核心函数**，**禁止**把主要算法逻辑只写在路由/Resource 里而不抽成独立函数。
- 文件保存为单文件 `{model_name}_algorithm.py` 即对应平台「单文件提交」；若依赖较多，在文件顶部用注释块列出 `requirements.txt` 风格依赖清单（每行一个包，尽量带版本号），便于用户后续打 ZIP 时复制。

### 4. 其他质量要求
- 语法正确，可直接 `python <文件名>.py` 运行（若仅提供 API，则 `if __name__ == '__main__':` 中启动 app 或提供最小调用示例）。
- 符合 PEP 8；适当的 try/except、输入校验与日志。
- 如有参考研究内容，在模块注释或 docstring 中标注参考来源。
- 不要使用省略号或 `pass` 代替真实实现。

---

## 代码生成清单（生成时必须满足）

1. 必要的导入语句
2. **至少一个带完整类型注解与 Google docstring 的独立核心函数**（含 `main_process` 或等价主入口）
3. 算法实现逻辑放在核心函数及其调用的辅助函数中；辅助函数若被 MCP 单独暴露，也需满足独立性要求
4. Flask + flask-restx 的 REST 层（薄层，调用核心函数）
5. `if __name__ == '__main__':` 中：可启动开发服务器，或演示对 `main_process` 的调用
6. 文件顶部或注释块中的 **pip 依赖清单**（推荐）

---

## 执行步骤（请严格按步骤执行）

### 步骤 1：分析需求并设计架构
仔细阅读用户需求、参考研究资料，设计：
- 哪些函数作为**独立、可封装**的核心 API（命名与参数列表）
- `main_process`（或等价入口）的职责边界
- Flask 路由如何**仅转发**到核心函数
在 thinking 中详细说明，并自检是否符合「无全局模型依赖」规范。

### 步骤 2：生成算法代码
基于步骤 1 生成完整 Python 单文件代码，**同时满足**上文「平台算法代码提交规范」与「代码生成清单」。

### 步骤 3：保存代码文件
使用内置 MCP 工具 **`stdio_built_in_file_saver`**（即 file_saver）将生成的完整代码保存。
- **推荐 file_path**：`temp/{model_name}_algorithm.py`（相对路径，会保存到平台 workspace 下的 temp 目录）
- 或使用绝对路径：`{WORKSPACE_ROOT}/temp/{model_name}_algorithm.py`

### 步骤 4：代码质量分析（六维 + 平台规范）
对生成的代码进行以下维度的分析评估：

1. **功能测试**：是否包含独立核心函数、`main_process`（或等价主入口）、完整类型注解、**Google 风格 docstring（含 Args/Returns）**；核心逻辑是否**不依赖**模块级全局模型/可变状态；类/函数定义与返回语句等
2. **平台提交规范**（单独一条）：对照《算法代码提交要求》逐项结论——函数独立性、Google docstring、主入口、类型注解、是否存在 global/模块级模型依赖风险、Web 层是否薄封装
3. **接口测试**：Flask/flask-restx 路由是否**薄封装**；请求参数处理与响应是否规范
4. **性能测试**：代码行数、结构复杂度、潜在性能问题
5. **可靠性测试**：异常处理（try/except）、日志、空值检查
6. **安全性测试**：是否使用 eval/exec/os.system 等危险函数
7. **兼容性测试**：Python 版本、依赖清单是否列出、跨平台注意事项

每个维度输出 status（passed/warning）、description 和 details；若违反「函数独立性」或缺少 Google docstring，**功能测试**或**平台提交规范**须标为 warning 并在 details 中说明。

### 步骤 5：保存最终结果（在调用 terminate 之前必须完成）
使用内置 MCP 工具 **`stdio_built_in_json_saver`**（即 json_saver），将下方格式的 **JSON 对象** 写入文件。
- **推荐 file_path**：`temp/aml_generate_result.json`
- 或使用绝对路径：`{WORKSPACE_ROOT}/temp/aml_generate_result.json`
- **禁止**在步骤 5 成功写入文件之前调用 terminate；`json_saver` 的 `content` 须为可被序列化的对象（可先写好 JSON 再解析为对象传入），确保含完整 `generated_code` 与 `test_results`。

结果 JSON 格式如下：
```json
{{{{
    "model_name": "{model_name}",
    "generated_code": "<步骤 2 生成的完整 Python 代码>",
    "code_filename": "{model_name}_algorithm.py",
    "test_results": [
        {{{{ "name": "功能测试", "status": "passed", "description": "...", "details": "..." }}}},
        {{{{ "name": "平台提交规范", "status": "passed", "description": "...", "details": "..." }}}},
        {{{{ "name": "接口测试", "status": "passed", "description": "...", "details": "..." }}}},
        {{{{ "name": "性能测试", "status": "passed", "description": "...", "details": "..." }}}},
        {{{{ "name": "可靠性测试", "status": "passed", "description": "...", "details": "..." }}}},
        {{{{ "name": "安全性测试", "status": "passed", "description": "...", "details": "..." }}}},
        {{{{ "name": "兼容性测试", "status": "passed", "description": "...", "details": "..." }}}}
    ],
    "references": [
        {{{{ "type": "paper", "title": "...", "summary": "..." }}}},
        {{{{ "type": "model", "title": "...", "summary": "..." }}}}
    ]
}}}}
```

### 步骤 6：完成任务
**仅在步骤 5 的 json 文件已成功保存后**，使用 **`stdio_built_in_terminate`**（即 terminate）结束，status 设为 `"success"`。

---

## 重要注意事项

1. **平台规范优先**：生成代码的首要目标是可被用户**原样或略作整理后**用于「垂域原子微服务发布」中的算法上传；**函数独立性**与 **Google docstring** 为硬约束，不可省略。
2. **代码完整性**：必须是完整可运行的实现，不要使用省略号、"此处省略"或 pass 代替真实逻辑
3. **逐步执行**：不要跳过任何步骤，严格按顺序执行
4. **错误处理**：如果工具调用失败，分析错误信息并重试（最多 3 次）
5. **JSON 格式**：确保保存的 JSON 格式正确，所有字符串正确转义
6. **参考来源**：如果使用了参考研究内容，在 references 字段中列出

现在开始执行任务，请从【步骤 1】开始。
"""
    return prompt
