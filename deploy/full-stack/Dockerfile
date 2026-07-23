# PC Remote Control 서버 컨테이너
# build context는 저장소 루트입니다 (docker-compose.yml에서 context: ../.. 지정).

FROM python:3.11-slim

# aiortc/PyAV(FFmpeg 코덱)와 X11 기반 화면 캡처(mss)·입력 주입(pyautogui, XTEST)에
# 필요한 시스템 라이브러리.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libx11-6 \
        libxext6 \
        libxtst6 \
        libxrandr2 \
        libxinerama1 \
        libxfixes3 \
        libopus0 \
        libvpx7 \
        xclip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY static ./static

EXPOSE 8000
ENTRYPOINT ["python", "server.py"]
