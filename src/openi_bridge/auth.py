"""Token resolution, mirroring the official openi CLI precedence rules."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_ENDPOINT = "https://openi.pcl.ac.cn"
TOKEN_FILE = Path.home() / ".openi" / "token.json"


@dataclass(frozen=True)
class Credentials:
    endpoint: str
    token: Optional[str]

    @property
    def has_token(self) -> bool:
        return bool(self.token)


def _read_token_file() -> dict:
    try:
        return json.loads(TOKEN_FILE.read_text())
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

