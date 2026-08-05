FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

ENV TZ=Asia/Shanghai

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends fonts-wqy-zenhei curl procps xvfb fluxbox x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

# 固定 requests 版本，保证镜像可复现构建（避免每次重建拉到不同版本）。
RUN pip install --no-cache-dir playwright==1.44.0 requests==2.32.3

COPY refresher.py .
COPY start.sh .
COPY session_browser.html /usr/share/novnc/session_browser.html

# 非 root 运行：playwright 基础镜像自带 pwuser，用它降权（缩小被 Chromium 渗透后的影响面）。
# /app 归属 pwuser，挂载进来的 /app/data 在运行时由 compose 处理写权限。
RUN chmod +x /app/start.sh && chown -R pwuser:pwuser /app
USER pwuser

# 内置 HTTP 服务健康检查；浏览器按账号请求时才启动，空闲时不占 Chromium 资源。
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6080/health', timeout=5)" || exit 1

CMD ["/app/start.sh"]
