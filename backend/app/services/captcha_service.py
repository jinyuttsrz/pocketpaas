"""图形验证码：内存会话存储 + 自绘 SVG 渲染。

设计要点：
- **零额外依赖**：与项目其余部分一致，不引入 Pillow / captcha 等第三方库，
  直接用标准库随机数绘制 SVG，前端以 data URI 直接展示；
- **内存存储**：uuid → (code, 过期时间)，TTL 默认 120 秒，一次性消费；
  单机部署（2H4G 线上 / WSL 本地）下足够，无需落库；
- **线程安全**：加锁保护，兼容 uvicorn 多线程与后台线程访问。

安全说明：验证码在账号密码之前校验，失败时不查库、不做口令哈希，
可用于阻挡暴力破解与账号枚举。
"""

from __future__ import annotations

import base64
import random
import threading
import time
import uuid

from app.core.config import settings

# 字符集剔除易混字符（0/O、1/I/L、2/Z 等），校验不区分大小写
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXY3456789"
_CODE_LEN = 4

# 验证码有效期（秒）：默认取自配置项
_TTL = 120

# 配色：与前端主题呼应的深色可读色系
_PALETTE = ["#7c3aed", "#059669", "#d97706", "#dc2626", "#2563eb"]


class CaptchaStore:
    """内存验证码会话（uuid → code / 过期时间），线程安全。"""

    def __init__(self, ttl: int = _TTL) -> None:
        self.ttl = ttl
        self._items: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def issue(self) -> dict:
        """生成一个新的验证码，返回 {uuid, img(data URI), expires_in}。"""
        code = "".join(random.choices(_ALPHABET, k=_CODE_LEN))
        uid = str(uuid.uuid4())
        with self._lock:
            self._items[uid] = (code, time.time() + self.ttl)
            self._sweep()
        return {"uuid": uid, "img": _render_svg(code), "expires_in": self.ttl}

    def verify(self, uid: str | None, code: str | None) -> str:
        """校验验证码，返回三态字符串。

        - ``"ok"``       通过（验证码一次性消费，校验后即失效）
        - ``"expired"``  uuid 不存在或已过期 → 提示刷新
        - ``"mismatch"`` uuid 有效但码不对 → 提示错误（不消费，允许重试）
        """
        if not uid or not code:
            return "mismatch"
        with self._lock:
            item = self._items.get(uid)
            if item is None or time.time() > item[1]:
                self._items.pop(uid, None)
                return "expired"
            if item[0].upper() != code.strip().upper():
                return "mismatch"
            self._items.pop(uid, None)
            return "ok"

    def _sweep(self) -> None:
        """清理已过期条目，避免内存无限增长（调用方需持有锁）。"""
        now = time.time()
        for key in [k for k, (_, exp) in self._items.items() if exp < now]:
            self._items.pop(key, None)


def _render_svg(code: str) -> str:
    """绘制验证码 SVG（干扰线 + 噪点 + 旋转字符），返回 base64 data URI。"""
    width, height = 120, 40
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="#f8fafc"/>',
    ]
    # 干扰线
    for _ in range(3):
        x1, y1 = random.randint(0, width - 1), random.randint(0, height - 1)
        x2, y2 = random.randint(0, width - 1), random.randint(0, height - 1)
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{random.choice(_PALETTE)}" stroke-width="1" opacity="0.35"/>'
        )
    # 噪点
    for _ in range(12):
        cx, cy = random.randint(0, width - 1), random.randint(0, height - 1)
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="1" '
            f'fill="{random.choice(_PALETTE)}" opacity="0.4"/>'
        )
    # 旋转字符
    step = width / (_CODE_LEN + 1)
    for i, ch in enumerate(code):
        x = step * (i + 1) + random.randint(-2, 2)
        y = height - 13 + random.randint(-2, 2)
        rot = random.randint(-18, 18)
        size = random.choice([21, 22, 23])
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" fill="{random.choice(_PALETTE)}" '
            f'font-size="{size}" font-family="Consolas,Menlo,monospace" font-weight="700" '
            f'text-anchor="middle" transform="rotate({rot} {x:.1f} {y:.1f})">{ch}</text>'
        )
    parts.append("</svg>")
    svg = "".join(parts)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# 进程级单例：所有请求共享同一份验证码会话（TTL 取配置项）
captcha_store = CaptchaStore(ttl=settings.AUTH_CAPTCHA_TTL)
