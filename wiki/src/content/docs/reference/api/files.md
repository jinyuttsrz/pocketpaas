---
title: 文件管理
description: 源码目录与容器文件系统双通道的浏览、在线编辑、上传下载、目录操作与体积上限。
---

## 接口清单

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/api/apps/{id}/files` | 浏览目录，`target=source` 或 `container` | 属主或管理员 |
| GET | `/api/apps/{id}/files/content` | 读取文本文件 | 属主或管理员 |
| PUT | `/api/apps/{id}/files/content` | 保存在线编辑内容 | 属主或管理员 |
| POST | `/api/apps/{id}/files/upload` | 上传文件，支持多选 | 属主或管理员 |
| POST | `/api/apps/{id}/files` | 新建目录、重命名或删除 | 属主或管理员 |
| GET | `/api/apps/{id}/files/download` | 下载文件或目录 zip | 属主或管理员 |

## 主要参数

- `target`：`source` 表示宿主源码目录，`container` 表示运行中的容器文件系统。
- `service`：`container` 目标对应的服务名；缺省取公开服务，多容器应用可指定内部服务。
- `path`：相对目标根目录的路径，根目录传空串。
- 写文件使用 `content` 字段，内容为 UTF-8 文本。
- 目录操作 `op` 取 `mkdir`、`rename` 或 `delete`，重命名时通过 `new_name` 指定新名称。

## 双通道说明

`target=source` 作用于宿主源码目录，修改随下次构建生效；
`target=container` 作用于运行中的容器文件系统，修改立即生效但重新部署后丢失。

所有路径先做规范化与越界校验，拒绝绝对路径与包含 `..` 的分段。

## 体积上限

| 场景 | 上限 |
|---|---|
| 在线编辑读取 | 512 KB |
| 单次上传 | 50 MB |
| 归档与下载 | 200 MB |

超过上限时接口拒绝请求并返回中文提示。目录下载会打包为 zip 流式返回。

## 相关页面

- 返回[接口总览](/reference/api/)
- 双通道与 Web 终端的使用说明：[项目文件与终端](/guide/files-terminal/)
