FROM python:3.12-slim

RUN echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm main contrib non-free non-free-firmware" > /etc/apt/sources.list \
    && echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-updates main contrib non-free non-free-firmware" >> /etc/apt/sources.list \
    && echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        git ssh openssh-server sudo vim tmux curl wget unzip zip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN mkdir -p /var/run/sshd \
    && echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config \
    && echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config \
    && echo 'root:Cwj010728' | chpasswd

WORKDIR /app

# Public runtime defaults. Compose/environment values can still override these.
# Keeping them in the image prevents legacy mounted config files from silently
# overriding the selected OpenRouter endpoint.
ENV LLM_MODEL="openrouter/qwen/qwen3.6-flash" \
    LLM_BASE_URL="https://openrouter.ai/api/v1" \
    LLM_REASONING_ENABLED="false"

COPY . /app

RUN pip install --no-cache-dir -e ".[all]" \
    && python -c "import orjson"

EXPOSE 8010 22 8000

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

CMD ["./start.sh"]
