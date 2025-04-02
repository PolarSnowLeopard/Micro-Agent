import math
from typing import Dict, List, Optional, Union, Tuple

import tiktoken
from openai import (
    APIError,
    AsyncOpenAI,
    AuthenticationError,
    OpenAIError,
    RateLimitError,
)
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from app.config import LLMSettings, config
from app.exceptions import TokenLimitExceeded
from app.logger import logger  # 假设你的应用中已设置了logger
from app.schema import (
    ROLE_VALUES,
    TOOL_CHOICE_TYPE,
    TOOL_CHOICE_VALUES,
    Message,
    ToolChoice,
    TokenUsage,
)


REASONING_MODELS = ["o1", "o3-mini"]
MULTIMODAL_MODELS = [
    "gpt-4-vision-preview",
    "gpt-4o",
    "gpt-4o-mini",
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
    "claude-3-haiku-20240307",
]


class TokenCounter:
    # Token常量
    BASE_MESSAGE_TOKENS = 4
    FORMAT_TOKENS = 2
    LOW_DETAIL_IMAGE_TOKENS = 85
    HIGH_DETAIL_TILE_TOKENS = 170

    # 图像处理常量
    MAX_SIZE = 2048
    HIGH_DETAIL_TARGET_SHORT_SIDE = 768
    TILE_SIZE = 512

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def count_text(self, text: str) -> int:
        """计算文本字符串的token数量"""
        return 0 if not text else len(self.tokenizer.encode(text))

    def count_image(self, image_item: dict) -> int:
        """
        根据详细程度和尺寸计算图像的token数量

        对于"低"详细度：固定85个tokens
        对于"高"详细度：
        1. 缩放以适应2048x2048方框
        2. 缩放最短边到768px
        3. 计算512px瓦片（每个170个tokens）
        4. 加上85个tokens
        """
        detail = image_item.get("detail", "medium")

        # 对于低详细度，始终返回固定的token数量
        if detail == "low":
            return self.LOW_DETAIL_IMAGE_TOKENS

        # 对于中等详细度（OpenAI默认），使用高详细度计算
        # OpenAI没有为中等详细度指定单独的计算方法

        # 对于高详细度，如果可用，则根据尺寸计算
        if detail == "high" or detail == "medium":
            # 如果在image_item中提供了尺寸
            if "dimensions" in image_item:
                width, height = image_item["dimensions"]
                return self._calculate_high_detail_tokens(width, height)

        # 当尺寸不可用或详细度级别未知时的默认值
        if detail == "high":
            # 默认使用1024x1024图像计算高详细度
            return self._calculate_high_detail_tokens(1024, 1024)  # 765 tokens
        elif detail == "medium":
            # 默认使用中等大小图像计算中等详细度
            return 1024  # 这与原始默认值匹配
        else:
            # 对于未知的详细度级别，使用中等作为默认值
            return 1024

    def _calculate_high_detail_tokens(self, width: int, height: int) -> int:
        """根据尺寸计算高详细度图像的tokens"""
        # 步骤1：缩放以适应MAX_SIZE x MAX_SIZE方框
        if width > self.MAX_SIZE or height > self.MAX_SIZE:
            scale = self.MAX_SIZE / max(width, height)
            width = int(width * scale)
            height = int(height * scale)

        # 步骤2：缩放使最短边为HIGH_DETAIL_TARGET_SHORT_SIDE
        scale = self.HIGH_DETAIL_TARGET_SHORT_SIDE / min(width, height)
        scaled_width = int(width * scale)
        scaled_height = int(height * scale)

        # 步骤3：计算512px瓦片的数量
        tiles_x = math.ceil(scaled_width / self.TILE_SIZE)
        tiles_y = math.ceil(scaled_height / self.TILE_SIZE)
        total_tiles = tiles_x * tiles_y

        # 步骤4：计算最终token数量
        return (
            total_tiles * self.HIGH_DETAIL_TILE_TOKENS
        ) + self.LOW_DETAIL_IMAGE_TOKENS

    def count_content(self, content: Union[str, List[Union[str, dict]]]) -> int:
        """计算消息内容的tokens"""
        if not content:
            return 0

        if isinstance(content, str):
            return self.count_text(content)

        token_count = 0
        for item in content:
            if isinstance(item, str):
                token_count += self.count_text(item)
            elif isinstance(item, dict):
                if "text" in item:
                    token_count += self.count_text(item["text"])
                elif "image_url" in item:
                    token_count += self.count_image(item)
        return token_count

    def count_tool_calls(self, tool_calls: List[dict]) -> int:
        """计算工具调用的tokens"""
        token_count = 0
        for tool_call in tool_calls:
            if "function" in tool_call:
                function = tool_call["function"]
                token_count += self.count_text(function.get("name", ""))
                token_count += self.count_text(function.get("arguments", ""))
        return token_count

    def count_message_tokens(self, messages: List[dict]) -> int:
        """计算消息列表中的总token数量"""
        total_tokens = self.FORMAT_TOKENS  # 基础格式tokens

        for message in messages:
            tokens = self.BASE_MESSAGE_TOKENS  # 每条消息的基础tokens

            # 添加角色tokens
            tokens += self.count_text(message.get("role", ""))

            # 添加内容tokens
            if "content" in message:
                tokens += self.count_content(message["content"])

            # 添加工具调用tokens
            if "tool_calls" in message:
                tokens += self.count_tool_calls(message["tool_calls"])

            # 添加name和tool_call_id的tokens
            tokens += self.count_text(message.get("name", ""))
            tokens += self.count_text(message.get("tool_call_id", ""))

            total_tokens += tokens

        return total_tokens


class LLM:
    _instances: Dict[str, "LLM"] = {}

    def __new__(
        cls, config_name: str = "default", llm_config: Optional[LLMSettings] = None
    ):
        if config_name not in cls._instances:
            instance = super().__new__(cls)
            instance.__init__(config_name, llm_config)
            cls._instances[config_name] = instance
        return cls._instances[config_name]

    def __init__(
        self, config_name: str = "default", llm_config: Optional[LLMSettings] = None
    ):
        if not hasattr(self, "client"):  # 仅在尚未初始化时进行初始化
            llm_config = llm_config or config.llm
            llm_config = llm_config.get(config_name, llm_config["default"])
            self.model = llm_config.model
            self.max_tokens = llm_config.max_tokens
            self.temperature = llm_config.temperature
            self.api_key = llm_config.api_key
            self.base_url = llm_config.base_url

            # 添加token计数相关属性
            self.total_input_tokens = 0
            self.total_completion_tokens = 0
            self.max_input_tokens = (
                llm_config.max_input_tokens
                if hasattr(llm_config, "max_input_tokens")
                else None
            )

            # 初始化tokenizer
            try:
                self.tokenizer = tiktoken.encoding_for_model(self.model)
            except KeyError:
                # 如果模型不在tiktoken的预设中，使用cl100k_base作为默认
                self.tokenizer = tiktoken.get_encoding("cl100k_base")

            # 初始化OpenAI客户端
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

            self.token_counter = TokenCounter(self.tokenizer)

    def count_tokens(self, text: str) -> int:
        """计算文本中的token数量"""
        if not text:
            return 0
        return len(self.tokenizer.encode(text))

    def count_message_tokens(self, messages: List[dict]) -> int:
        return self.token_counter.count_message_tokens(messages)

    def update_token_count(self, input_tokens: int, completion_tokens: int = 0) -> None:
        """更新token计数"""
        # 仅在设置了max_input_tokens时跟踪tokens
        self.total_input_tokens += input_tokens
        self.total_completion_tokens += completion_tokens
        logger.info(
            f"Token使用情况: 输入={input_tokens}, 输出={completion_tokens}, "
            f"累计输入={self.total_input_tokens}, 累计输出={self.total_completion_tokens}, "
            f"总计={input_tokens + completion_tokens}, 累计总计={self.total_input_tokens + self.total_completion_tokens}"
        )

    def check_token_limit(self, input_tokens: int) -> bool:
        """检查是否超过token限制"""
        if self.max_input_tokens is not None:
            return (self.total_input_tokens + input_tokens) <= self.max_input_tokens
        # 如果未设置max_input_tokens，始终返回True
        return True

    def get_limit_error_message(self, input_tokens: int) -> str:
        """生成超出token限制的错误消息"""
        if (
            self.max_input_tokens is not None
            and (self.total_input_tokens + input_tokens) > self.max_input_tokens
        ):
            return f"请求可能超出输入token限制（当前：{self.total_input_tokens}，需要：{input_tokens}，最大：{self.max_input_tokens}）"

        return "超出Token限制"

    @staticmethod
    def format_messages(
        messages: List[Union[dict, Message]], supports_images: bool = False
    ) -> List[dict]:
        """
        将消息格式化为LLM，通过将它们转换为OpenAI消息格式。

        参数:
            messages: 消息列表，可以是dict或Message对象
            supports_images: 指示目标模型是否支持图像输入的标志

        返回:
            List[dict]: OpenAI格式的格式化消息列表

        异常:
            ValueError: 如果消息无效或缺少必填字段
            TypeError: 如果提供了不支持的消息类型

        示例:
            >>> msgs = [
            ...     Message.system_message("You are a helpful assistant"),
            ...     {"role": "user", "content": "Hello"},
            ...     Message.user_message("How are you?")
            ... ]
            >>> formatted = LLM.format_messages(msgs)
        """
        formatted_messages = []

        for message in messages:
            # 将Message对象转换为字典
            if isinstance(message, Message):
                message = message.to_dict()

            if isinstance(message, dict):
                # 如果消息是字典，确保它具有必需的字段
                if "role" not in message:
                    raise ValueError("消息字典必须包含'role'字段")

                # 如果存在base64图像且模型支持图像，则处理
                if supports_images and message.get("base64_image"):
                    # 初始化或转换内容为适当的格式
                    if not message.get("content"):
                        message["content"] = []
                    elif isinstance(message["content"], str):
                        message["content"] = [
                            {"type": "text", "text": message["content"]}
                        ]
                    elif isinstance(message["content"], list):
                        # 将字符串项转换为适当的文本对象
                        message["content"] = [
                            (
                                {"type": "text", "text": item}
                                if isinstance(item, str)
                                else item
                            )
                            for item in message["content"]
                        ]

                    # 将图像添加到内容中
                    message["content"].append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{message['base64_image']}"
                            },
                        }
                    )

                    # 删除base64_image字段
                    del message["base64_image"]
                # 如果模型不支持图像但消息中有base64_image，优雅地处理
                elif not supports_images and message.get("base64_image"):
                    # 只删除base64_image字段并保留文本内容
                    del message["base64_image"]

                if "content" in message or "tool_calls" in message:
                    formatted_messages.append(message)
                # else: 不包含该消息
            else:
                raise TypeError(f"不支持的消息类型: {type(message)}")

        # 验证所有消息都具有必需的字段
        for msg in formatted_messages:
            if msg["role"] not in ROLE_VALUES:
                raise ValueError(f"无效的角色: {msg['role']}")

        return formatted_messages

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        retry=retry_if_exception_type(
            (OpenAIError, Exception, ValueError)
        ),  # 不要重试TokenLimitExceeded
    )
    async def ask(
        self,
        messages: List[Union[dict, Message]],
        system_msgs: Optional[List[Union[dict, Message]]] = None,
        stream: bool = True,
        temperature: Optional[float] = None,
    ) -> str:
        """
        向LLM发送提示并获取响应。

        参数:
            messages: 对话消息列表
            system_msgs: 可选的系统消息，用于预置
            stream (bool): 是否流式传输响应
            temperature (float): 响应的采样温度

        返回:
            str: 生成的响应

        异常:
            TokenLimitExceeded: 如果超出token限制
            ValueError: 如果消息无效或响应为空
            OpenAIError: 如果API调用在重试后失败
            Exception: 对于意外错误
        """
        try:
            # 检查模型是否支持图像
            supports_images = self.model in MULTIMODAL_MODELS

            # 使用图像支持检查格式化系统和用户消息
            if system_msgs:
                system_msgs = self.format_messages(system_msgs, supports_images)
                messages = system_msgs + self.format_messages(messages, supports_images)
            else:
                messages = self.format_messages(messages, supports_images)

            # 计算输入token数量
            input_tokens = self.count_message_tokens(messages)

            # 检查是否超过token限制
            if not self.check_token_limit(input_tokens):
                error_message = self.get_limit_error_message(input_tokens)
                # 引发一个不会被重试的特殊异常
                raise TokenLimitExceeded(error_message)

            params = {
                "model": self.model,
                "messages": messages,
            }

            if self.model in REASONING_MODELS:
                params["max_completion_tokens"] = self.max_tokens
            else:
                params["max_tokens"] = self.max_tokens
                params["temperature"] = (
                    temperature if temperature is not None else self.temperature
                )

            if not stream:
                # 非流式请求
                response = await self.client.chat.completions.create(
                    **params, stream=False
                )

                if not response.choices or not response.choices[0].message.content:
                    raise ValueError("来自LLM的空或无效响应")

                # 更新token计数
                self.update_token_count(
                    response.usage.prompt_tokens, response.usage.completion_tokens
                )

                return response.choices[0].message.content

            # 流式请求，对于流式传输，在发出请求前更新估计的token计数
            self.update_token_count(input_tokens)

            response = await self.client.chat.completions.create(**params, stream=True)

            collected_messages = []
            completion_text = ""
            async for chunk in response:
                chunk_message = chunk.choices[0].delta.content or ""
                collected_messages.append(chunk_message)
                completion_text += chunk_message
                print(chunk_message, end="", flush=True)

            print()  # 流式传输后的换行
            full_response = "".join(collected_messages).strip()
            if not full_response:
                raise ValueError("流式LLM响应为空")

            # 估计流式响应的完成tokens
            completion_tokens = self.count_tokens(completion_text)
            logger.info(
                f"流式响应的估计完成tokens: {completion_tokens}"
            )
            self.total_completion_tokens += completion_tokens

            return full_response

        except TokenLimitExceeded:
            # 重新引发token限制错误，不记录日志
            raise
        except ValueError:
            logger.exception(f"验证错误")
            raise
        except OpenAIError as oe:
            logger.exception(f"OpenAI API错误")
            if isinstance(oe, AuthenticationError):
                logger.error("身份验证失败。请检查API密钥。")
            elif isinstance(oe, RateLimitError):
                logger.error("超出速率限制。考虑增加重试尝试次数。")
            elif isinstance(oe, APIError):
                logger.error(f"API错误: {oe}")
            raise
        except Exception:
            logger.exception(f"ask中的意外错误")
            raise

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        retry=retry_if_exception_type(
            (OpenAIError, Exception, ValueError)
        ),  # 不要重试TokenLimitExceeded
    )
    async def ask_with_images(
        self,
        messages: List[Union[dict, Message]],
        images: List[Union[str, dict]],
        system_msgs: Optional[List[Union[dict, Message]]] = None,
        stream: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """
        向LLM发送带有图像的提示并获取响应。

        参数:
            messages: 对话消息列表
            images: 图像URL或图像数据字典列表
            system_msgs: 可选的系统消息，用于预置
            stream (bool): 是否流式传输响应
            temperature (float): 响应的采样温度

        返回:
            str: 生成的响应

        异常:
            TokenLimitExceeded: 如果超出token限制
            ValueError: 如果消息无效或响应为空
            OpenAIError: 如果API调用在重试后失败
            Exception: 对于意外错误
        """
        try:
            # 对于ask_with_images，我们总是将supports_images设置为True，因为
            # 此方法应该只用于支持图像的模型
            if self.model not in MULTIMODAL_MODELS:
                raise ValueError(
                    f"模型 {self.model} 不支持图像。请使用以下模型之一: {MULTIMODAL_MODELS}"
                )

            # 使用图像支持格式化消息
            formatted_messages = self.format_messages(messages, supports_images=True)

            # 确保最后一条消息来自用户，以附加图像
            if not formatted_messages or formatted_messages[-1]["role"] != "user":
                raise ValueError(
                    "最后一条消息必须来自用户才能附加图像"
                )

            # 处理最后一条用户消息以包含图像
            last_message = formatted_messages[-1]

            # 将内容转换为多模态格式（如果需要）
            content = last_message["content"]
            multimodal_content = (
                [{"type": "text", "text": content}]
                if isinstance(content, str)
                else content
                if isinstance(content, list)
                else []
            )

            # 将图像添加到内容中
            for image in images:
                if isinstance(image, str):
                    multimodal_content.append(
                        {"type": "image_url", "image_url": {"url": image}}
                    )
                elif isinstance(image, dict) and "url" in image:
                    multimodal_content.append({"type": "image_url", "image_url": image})
                elif isinstance(image, dict) and "image_url" in image:
                    multimodal_content.append(image)
                else:
                    raise ValueError(f"不支持的图像格式: {image}")

            # 使用多模态内容更新消息
            last_message["content"] = multimodal_content

            # 如果提供了系统消息，则添加
            if system_msgs:
                all_messages = (
                    self.format_messages(system_msgs, supports_images=True)
                    + formatted_messages
                )
            else:
                all_messages = formatted_messages

            # 计算tokens并检查限制
            input_tokens = self.count_message_tokens(all_messages)
            if not self.check_token_limit(input_tokens):
                raise TokenLimitExceeded(self.get_limit_error_message(input_tokens))

            # 设置API参数
            params = {
                "model": self.model,
                "messages": all_messages,
                "stream": stream,
            }

            # 添加特定于模型的参数
            if self.model in REASONING_MODELS:
                params["max_completion_tokens"] = self.max_tokens
            else:
                params["max_tokens"] = self.max_tokens
                params["temperature"] = (
                    temperature if temperature is not None else self.temperature
                )

            # 处理非流式请求
            if not stream:
                response = await self.client.chat.completions.create(**params)

                if not response.choices or not response.choices[0].message.content:
                    raise ValueError("来自LLM的空或无效响应")

                self.update_token_count(response.usage.prompt_tokens)
                return response.choices[0].message.content

            # 处理流式请求
            self.update_token_count(input_tokens)
            response = await self.client.chat.completions.create(**params)

            collected_messages = []
            async for chunk in response:
                chunk_message = chunk.choices[0].delta.content or ""
                collected_messages.append(chunk_message)
                print(chunk_message, end="", flush=True)

            print()  # 流式传输后的换行
            full_response = "".join(collected_messages).strip()

            if not full_response:
                raise ValueError("流式LLM响应为空")

            return full_response

        except TokenLimitExceeded:
            raise
        except ValueError as ve:
            logger.error(f"ask_with_images中的验证错误: {ve}")
            raise
        except OpenAIError as oe:
            logger.error(f"OpenAI API错误: {oe}")
            if isinstance(oe, AuthenticationError):
                logger.error("身份验证失败。请检查API密钥。")
            elif isinstance(oe, RateLimitError):
                logger.error("超出速率限制。考虑增加重试尝试次数。")
            elif isinstance(oe, APIError):
                logger.error(f"API错误: {oe}")
            raise
        except Exception as e:
            logger.error(f"ask_with_images中的意外错误: {e}")
            raise

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        retry=retry_if_exception_type(
            (OpenAIError, Exception, ValueError)
        ),  # 不要重试TokenLimitExceeded
    )
    async def ask_tool(
        self,
        messages: List[Union[dict, Message]],
        system_msgs: Optional[List[Union[dict, Message]]] = None,
        timeout: int = 300,
        tools: Optional[List[dict]] = None,
        tool_choice: TOOL_CHOICE_TYPE = ToolChoice.AUTO,  # type: ignore
        temperature: Optional[float] = None,
        **kwargs,
    ) -> Tuple[ChatCompletionMessage | None, TokenUsage]:
        """
        使用函数/工具询问LLM并返回响应。

        参数:
            messages: 对话消息列表
            system_msgs: 可选的系统消息，用于预置
            timeout: 请求超时（秒）
            tools: 要使用的工具列表
            tool_choice: 工具选择策略
            temperature: 响应的采样温度
            **kwargs: 额外的完成参数

        返回:
            ChatCompletionMessage: 模型的响应

        异常:
            TokenLimitExceeded: 如果超出token限制
            ValueError: 如果工具、tool_choice或消息无效
            OpenAIError: 如果API调用在重试后失败
            Exception: 对于意外错误
        """
        try:
            # 验证tool_choice
            if tool_choice not in TOOL_CHOICE_VALUES:
                raise ValueError(f"无效的tool_choice: {tool_choice}")

            # 检查模型是否支持图像
            supports_images = self.model in MULTIMODAL_MODELS

            # 格式化消息
            if system_msgs:
                system_msgs = self.format_messages(system_msgs, supports_images)
                messages = system_msgs + self.format_messages(messages, supports_images)
            else:
                messages = self.format_messages(messages, supports_images)

            # 计算输入token数量
            input_tokens = self.count_message_tokens(messages)

            # 如果有工具，计算工具描述的token数量
            tools_tokens = 0
            if tools:
                for tool in tools:
                    tools_tokens += self.count_tokens(str(tool))

            input_tokens += tools_tokens

            # 检查是否超过token限制
            if not self.check_token_limit(input_tokens):
                error_message = self.get_limit_error_message(input_tokens)
                # 引发一个不会被重试的特殊异常
                raise TokenLimitExceeded(error_message)

            # 如果提供了工具，则验证
            if tools:
                for tool in tools:
                    if not isinstance(tool, dict) or "type" not in tool:
                        raise ValueError("每个工具必须是带有'type'字段的字典")

            # 设置完成请求
            params = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "timeout": timeout,
                **kwargs,
            }

            if self.model in REASONING_MODELS:
                params["max_completion_tokens"] = self.max_tokens
            else:
                params["max_tokens"] = self.max_tokens
                params["temperature"] = (
                    temperature if temperature is not None else self.temperature
                )

            response: ChatCompletion = await self.client.chat.completions.create(
                **params, stream=False
            )

            # 检查响应是否有效
            if not response.choices or not response.choices[0].message:
                print(response)
                # raise ValueError("来自LLM的无效或空响应")
                return None

            # 更新token计数
            self.update_token_count(
                response.usage.prompt_tokens, response.usage.completion_tokens
            )

            token_usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
            )

            return response.choices[0].message, token_usage

        except TokenLimitExceeded:
            # 重新引发token限制错误，不记录日志
            raise
        except ValueError as ve:
            logger.error(f"ask_tool中的验证错误: {ve}")
            raise
        except OpenAIError as oe:
            logger.error(f"OpenAI API错误: {oe}")
            if isinstance(oe, AuthenticationError):
                logger.error("身份验证失败。请检查API密钥。")
            elif isinstance(oe, RateLimitError):
                logger.error("超出速率限制。考虑增加重试尝试次数。")
            elif isinstance(oe, APIError):
                logger.error(f"API错误: {oe}")
            raise
        except Exception as e:
            logger.error(f"ask_tool中的意外错误: {e}")
            raise
