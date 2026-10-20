#!/bin/sh
# PocketPaaS 容器入口：先拉起 nginx，再启动平台本体。
#
# 为什么需要常驻 nginx：控制台域名（如 app.{BASE_DOMAIN}）由 nginx 反代到
# 平台自身的 8000 端口，只有 nginx 从容器启动就监听 80，面板域名才可用，
# 而不必等首次部署应用时由 route_service 临时拉起。
#
# 失败处理：nginx 起不来不阻塞平台启动——数据面（容器管理）优先于控制面（路由），
# 与 route_service 中"路由同步失败仅告警"的降级策略保持一致。
set -e

if [ "${ENABLE_NGINX}" = "true" ]; then
    if nginx -t >/dev/null 2>&1; then
        nginx || echo "[entrypoint] nginx 启动失败，路由功能不可用"
    else
        echo "[entrypoint] nginx 配置校验未通过，跳过启动，详情："
        nginx -t || true
    fi
fi

exec "$@"