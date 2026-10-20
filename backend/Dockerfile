# PocketPaaS 平台镜像
#
# 平台自身要管理宿主 Docker 并做动态路由，故镜像内需要：
# - nginx ：route_service 直接调用 nginx / nginx -t / nginx -s reload
# - git   ：source_service 拉取 Git 源码
# - procps：route_service 用 pgrep 判断 nginx 是否在运行
# - curl  ：容器健康检查
FROM python:3.12-slim

# 国内加速源（与项目约定一致）：海外服务器可 --build-arg 覆盖为官方源
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 替换 Debian 源为国内镜像，避免 deb.debian.org 超时导致构建极慢
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nginx git procps curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    # 移除 nginx 默认站点，避免它抢占 80 端口 / 与平台路由块冲突
    && rm -f /etc/nginx/sites-enabled/default

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

# 零构建静态 SPA，只需拷贝应用代码
COPY app ./app

# 入口脚本：容器启动时先拉起 nginx（面板域名路由需要），再启动平台
# sed 去除 CRLF：防止 Windows 下编辑后出现 "bad interpreter" 报错
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh \
    && chmod +x /usr/local/bin/entrypoint.sh

# 数据目录（数据库 / 构建工作区）与 nginx 路由目录
RUN mkdir -p /data/workspace /etc/nginx/conf.d

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# 单进程运行：验证码为内存态、指标/巡检为进程内线程，多 worker 会导致状态不一致
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
