# veya-video-sandbox — 视频质检沙箱 (只读 ffprobe 规则质检)
# 用法: docker build -t veya-video-sandbox:latest infra/video_sandbox
# 运行: docker run --rm -i --network=none -v <dir>:/work:ro veya-video-sandbox <<'JSON'
#   {"video_path": "/work/input.mp4", "spec": {...}}
# JSON

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 10001 sandbox \
 && pip install --no-cache-dir orjson

WORKDIR /work
USER sandbox
COPY evaluate.py /usr/local/bin/evaluate.py

# 只读 + 无网络 (由 compose/运行时 --network=none 或同机资源限制保证)。
ENTRYPOINT ["python", "/usr/local/bin/evaluate.py"]
