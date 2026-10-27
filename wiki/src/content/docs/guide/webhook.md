---
title: Git Webhook 自动部署
description: 用 HMAC 签名的 Webhook 把 Git 推送接入「拉源码 → 重建镜像 → 重新部署」流水线。
---

配置 Webhook 后，Git 平台的一次推送即可触发自动部署，适合持续交付演示与个人项目托管。

## 流水线

```text
Git 推送 → Webhook 接收（签名校验） → 拉取源码 → 重建镜像 → 重新部署
```

:::tip[构建期间旧容器继续服务]
流水线在构建完成前不会停止旧容器，业务流量不中断；新镜像就绪后才切换。
:::

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/apps/{app_id}/webhook` | 接收 Git 推送（匿名可访问，但必须通过签名校验） |
| GET | `/api/apps/{app_id}/webhook` | 查看 Webhook 配置 |
| POST | `/api/apps/{app_id}/webhook/reset` | 重置签名密钥 |
| POST | `/api/apps/{app_id}/webhook/trigger` | 手动触发一次自动部署 |

## 签名校验

接收端点匿名可访问，但请求必须携带可验证的签名：

| 来源 | 方式 |
|---|---|
| GitHub 风格 | `X-Hub-Signature-256`：HMAC-SHA256 |
| 通用 | `X-Paas-Signature`：HMAC-SHA256 |
| GitLab | `X-Gitlab-Token`：明文比对 |

签名不匹配的请求会被拒绝，不会触发构建。

:::caution[密钥泄露后立即重置]
调用 `POST /api/apps/{app_id}/webhook/reset` 会生成新密钥，
记得同步更新 Git 平台侧的 Webhook 配置，否则后续推送会被拒绝。
:::

## 并发保护

平台使用**进程内并发锁**：

- 同一应用的 Webhook 触发是串行的，避免重复构建；
- 构建进行中再次收到推送会返回 `409`，不会排队堆积。

:::note[单进程前提]
并发锁是进程内实现，与平台「单进程运行」的部署方式一致。
若改用多 worker 运行，锁不再全局有效。
:::

## 配置步骤

1. 在控制台打开应用详情，找到 Webhook 配置并复制接收地址与密钥；
2. 在 Git 平台仓库设置中新增 Webhook：
   - Payload URL 填接收地址；
   - Content type 选 `application/json`；
   - Secret 填平台提供的密钥（GitLab 则填 Token）；
3. 推送一次代码，观察构建日志；
4. 也可用 `POST /api/apps/{app_id}/webhook/trigger` 手动触发验证链路。

## 与手动部署的区别

| 项 | Webhook 自动部署 | 控制台手动部署 |
|---|---|---|
| 触发方 | Git 平台推送 | 人工点按钮 |
| 源码来源 | 应用已绑定的 Git 仓库 | 已上传的源码包 |
| 构建期间服务 | 旧容器继续服务 | 旧容器继续服务 |
| 审计记录 | 记录 Webhook 相关动作 | 记录部署动作 |

## 相关接口

端点、签名算法与密钥管理见[Webhook 自动部署](/reference/api/webhook/)分册。
