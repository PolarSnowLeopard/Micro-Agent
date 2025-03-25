FROM python:3.12-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    git \
    ssh \
    sudo \
    && rm -rf /var/lib/apt/lists/*
 
# Set the working directory in the container 
WORKDIR /app 
 
# Copy the current directory contents into the container at /app 
COPY . /app 
 
# Install any needed packages specified in requirements.txt 
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
 
# Make port 5000 available to the world outside this container 
EXPOSE 5000
 
# Run app.py when the container launches 
CMD ["bash"] 
