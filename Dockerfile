FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget gosu && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app/ ./app/
COPY static/ ./static/
# 烘焙 api/ 热更新资源（QR 配置/图片）进镜像，使镜像自包含：
# 即使 docker run 不挂载 ./api:/app/api，/app/api 也存在，避免 StaticFiles 启动崩溃；
# compose 的 bind mount 仍可在运行时覆盖这些默认资源。
# 注意：仓库 .dockerignore 默认排除 api，发布流水线会在构建前临时取消该排除。
COPY api/ ./api/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# 非 root 运行：缩小容器被攻破后的影响面（VULN 加固）。uid 1000 与常见宿主用户、
# refresher 的 pwuser 对齐，使共享的 ./data 属主一致。容器以 root 启动后由
# docker-entrypoint.sh 修复 bind 挂载卷属主再 gosu 降权到 appuser——这样既非 root 运行，
# 又让历史部署（data 由旧 root 容器创建）`docker compose pull` 无缝升级，无需手动 chown。
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 5918

# 镜像内健康检查：plain docker run 也能获得 liveness 信号（compose 另有 healthcheck）。
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD wget -q -O /dev/null http://localhost:5918/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5918"]
