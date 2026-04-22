# 使用轻量级 Python 镜像
FROM python:3.11-slim

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV TZ=Asia/Shanghai

# 设置工作目录
WORKDIR /app

# 安装系统依赖 (MySQL 客户端开发库等)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    default-libmysqlclient-dev \
    pkg-config \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 拷贝依赖并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝项目文件
COPY . .

# 创建媒体文件目录
RUN mkdir -p media/products

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
