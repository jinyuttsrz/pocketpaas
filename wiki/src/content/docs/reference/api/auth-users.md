---
title: 认证与用户
description: 登录、注册、图形验证码、会话注销以及管理员用户管理接口。
---

## 认证接口

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/api/auth/status` | 登录态探测，返回是否登录与验证码开关 | 匿名 |
| GET | `/api/auth/captcha` | 获取图形验证码，返回 uuid 与 SVG 图片 | 匿名 |
| POST | `/api/auth/register` | 注册并自动登录，校验验证码 | 匿名 |
| POST | `/api/auth/login` | 登录，校验验证码并签发会话令牌 | 匿名 |
| POST | `/api/auth/logout` | 登出并注销当前会话 | 登录用户 |
| GET | `/api/auth/me` | 返回当前登录用户信息 | 登录用户 |

## 登录示例

```http
POST /api/auth/login
Content-Type: application/json

{
  "username": "admin",
  "password": "********",
  "captcha_uuid": "0f5c...",
  "captcha_code": "A7K2"
}
```

登录成功后返回 `token`、`expires_in` 与 `user` 信息，并在响应头写入
`Set-Cookie: paas_token=...; HttpOnly; SameSite=Lax`。后续请求可自动携带该 Cookie，
也可改用 Bearer 头。

注册请求包含 `username`、`password`、可选 `display_name`、`captcha_uuid` 与 `captcha_code`；
管理员新建用户包含 `username`、`password`、`display_name` 与 `role`。

:::note[验证码配置]
验证码由 `AUTH_ENABLE_CAPTCHA` 控制，默认有效期由 `AUTH_CAPTCHA_TTL` 指定，默认 120 秒。
验证码以 SVG 返回，登录与注册时需回传 `captcha_uuid` 与 `captcha_code`。
:::

## 用户管理接口

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/api/users` | 用户列表 | 管理员 |
| POST | `/api/users` | 新建用户，可指定角色 | 管理员 |
| PATCH | `/api/users/{id}` | 修改显示名、角色或启用状态 | 管理员 |
| PUT | `/api/users/{id}/password` | 重置用户口令 | 管理员 |
| DELETE | `/api/users/{id}` | 删除用户，含最后管理员保护 | 管理员 |

平台至少保留一名可用管理员。删除或降级最后一个管理员时接口返回 400。

## 相关页面

- 返回[接口总览](/reference/api/)
