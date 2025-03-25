FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .

# 使用清华源安装依赖
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["python", "app.py"]