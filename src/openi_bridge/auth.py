"""Token resolution, mirroring the official openi CLI precedence rules."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_ENDPOINT = "https://openi.pcl.ac.cn"


def token_file() -> Path:
    """令牌文件的位置，每次都按当前的 HOME 算。

    不要把它缓存成模块级常量：那样 ``HOME`` 变了（测试隔离、或用户在同一个
    进程里切配置）也还读旧路径，而读的是哪个令牌文件是这里最要紧的事情。
    """
    return Path.home() / ".openi" / "token.json"


# 兼容旧引用；真正读取请用 token_file()，它按当前 HOME 重新计算。
TOKEN_FILE = token_file()


@dataclass(frozen=True)
class Credentials:
    endpoint: str
    token: Optional[str]

    @property
    def has_token(self) -> bool:
        return bool(self.token)


def _read_token_file() -> dict:
    try:
        return json.loads(token_file().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def resolve_credentials(
    token: Optional[str] = None, endpoint: Optional[str] = None
) -> Credentials:
    """Resolve credentials with the same precedence as the official CLI.

    Explicit arguments win, then ~/.openi/token.json, then the environment.
    """
    saved = _read_token_file()
    resolved_token = token or saved.get("token") or os.getenv("OPENI_TOKEN") or None
    resolved_endpoint = (
        endpoint
        or saved.get("endpoint")
        or os.getenv("OPENI_ENDPOINT")
        or DEFAULT_ENDPOINT
    )
    return Credentials(endpoint=resolved_endpoint.rstrip("/"), token=resolved_token)

