---
title: Webhook 自动部署
description: Git 推送回调的端点、签名校验方式、密钥管理、手动触发与流水线并发规则。
---

## 接口清单

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/api/apps/{id}/webhook` | 接收 Git 平台推送，签名校验 | 匿名 |
| GET | `/api/apps/{id}/webhook` | 查看 Webhook 配置 | 属主或管理员 |
| POST | `/api/apps/{id}/webhook/reset` | 重置签名密钥 | 属主或管理员 |
| POST | `/api/apps/{id}/webhook/trigger` | 手动触发一次自动部署 | 属主或管理员 |

## 签名校验

回调端点为匿名接口，但必须通过签名校验。平台兼容：

- GitHub 与 Gitea 的 `X-Hub-Signature-256`
- 自定义 `X-Paas-Signature`
- GitLab 的 `X-Gitlab-Token`

每个应用持有独立密钥，可随时重置。

## 流水线

校验通过后，平台在后台线程串行执行重新拉取源码、重建镜像与重新部署。
重建镜像期间旧容器继续对外服务，仅在新容器部署时移除旧容器。

同一应用同时只允许一条流水线，重复触发返回 409。

## 相关页面

- 返回[接口总览](/reference/api/)
- 配置步骤与手动部署对比：[Git Webhook 自动部署](/guide/webhook/)
