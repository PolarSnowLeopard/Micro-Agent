FROM python:3.12-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    git \
    ssh \
    openssh-server \
    sudo \
    vim \
    tmux \
    && rm -rf /var/lib/apt/lists/*

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
RUN pip install --no-cache-dir -r requirements.txt 
# -i https://pypi.tuna.tsinghua.edu.cn/simple
 
# Make port 5000 available to the world outside this container 
EXPOSE 5000 22 8000
 
# 启动SSH服务并保持容器运行
CMD service ssh start && bash