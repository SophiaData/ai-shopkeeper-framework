FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin shopkeeper

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && mkdir -p /app/artifacts \
    && chown -R shopkeeper:shopkeeper /app

COPY --chown=shopkeeper:shopkeeper prompts ./prompts
COPY --chown=shopkeeper:shopkeeper scripts ./scripts
COPY --chown=shopkeeper:shopkeeper docs ./docs

# 容器内听 0.0.0.0，才能接到 compose 映射进来的连接。
# 宿主机端口由 docker-compose 绑在 127.0.0.1，不要改成对局域网开放。
# 默认 8780：与本机开发常用的 8000 错开，避免 Windows svchost 占用。
ENV PYTHONUNBUFFERED=1 \
    WORKFLOW_HOST=0.0.0.0 \
    WORKFLOW_PORT=8780 \
    WORKFLOW_SCHEDULER=1

USER shopkeeper
EXPOSE 8780

# 无鉴权 /healthz；端口随 WORKFLOW_PORT。不安装 curl，用标准库探活。
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WORKFLOW_PORT','8780'), timeout=4)"

CMD ["python", "-m", "shopkeeper.workflow.serve"]
