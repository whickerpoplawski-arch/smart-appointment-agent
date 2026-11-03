"""trace 落盘前的脱敏（P3 spec §6.4）——**唯一实现**。

密钥与 token 一旦进了 runs/ 或事件文件就可能被提交，所以这里是唯一出口。
`evals/sanitize.py` 只做再导出（`redact is evals.sanitize.redact`），不得再写第二份规则。
"""

from __future__ import annotations

import re

RULES = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(appid=)[^&\s'\"]+"), r"\1***"),
    (re.compile(r"((?:api[_-]?key|apikey|token|secret|password)\s*[=:]\s*)[^\s&,'\"]+", re.IGNORECASE),
     r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r"(\"(?:Authorization|api-key|api_key|Authorization)\"\s*:\s*\")[^\"]+", re.IGNORECASE),
     r"\1***"),
    # 完整 URL 查询串里的自由度：只保留 host/path
    (re.compile(r"(https?://[^?\s]+)\?[^\s'\"]+"), r"\1?***"),
)


def redact_text(text: str) -> str:
    """对单段文本套用全部脱敏规则。"""
    if not text:
        return text
    result = str(text)
    for pattern, replacement in RULES:
        result = pattern.sub(replacement, result)
    return result


def redact(obj):
    """递归脱敏 dict / list / str；其他类型原样返回。"""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {redact_text(str(key)): redact(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(item) for item in obj]
    return obj


def redact_exception(exc: BaseException) -> str:
    """异常只记类型 + 脱敏消息，不记完整 repr / traceback。"""
    return f"{type(exc).__name__}: {redact_text(str(exc))}"


__all__ = ["RULES", "redact", "redact_text", "redact_exception"]
