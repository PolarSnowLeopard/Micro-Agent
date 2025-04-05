FROM python:3.12-slim

# 创建 sources.list 并配置清华源
RUN echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm main contrib non-free non-free-firmware" > /etc/apt/sources.list \
    && echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-updates main contrib non-free non-free-firmware" >> /etc/apt/sources.list \
    && echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-backports main contrib non-free non-free-firmware" >> /etc/apt/sources.list \
    && echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ssh \
        openssh-server \
        sudo \
        vim \
        tmux \
        vim-runtime \
        xauth \
        xxd \
        curl \
        wget \
        unzip \
        zip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 配置SSH服务
RUN mkdir -p /var/run/sshd \
    && echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config \
    && echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config \
    && echo 'root:Cwj010728' | chpasswd  
 
# Set the working directory in the container 
WORKDIR /app 
 
# Copy the current directory contents into the container at /app 
COPY . /app 
 
# Install any needed packages specified in requirements.txt 
RUN pip install -r requirements.txt 
# -i https://pypi.tuna.tsinghua.edu.cn/simple
 
# Make port 5000 available to the world outside this container 
EXPOSE 8010 22 8000
 
# 启动SSH服务并保持容器运行
CMD service ssh start && bash