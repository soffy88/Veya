FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-dev \
    git \
    build-essential \
    z3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip3 install --no-cache-dir \
    crosshair-tool deal \
    networkx scipy numpy pandas \
    paho-mqtt fastapi uvicorn

COPY . /app/

# 修改默认启动命令为常驻 Uvicorn 服务
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
