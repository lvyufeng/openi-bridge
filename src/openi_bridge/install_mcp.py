"""把 openi-bridge 注册到 Claude Code 与 Codex，让任意目录都能直接调用。

MCP 服务的启动方式只有一条硬要求：客户端要能从一个**绝对路径**把它拉起来，
且它不依赖当前工作目录。这个模块负责把这两件事做掉——把启动命令算成绝对路径，
再写进两边的配置：

    Claude Code   ~/.claude.json 的 mcpServers（通过 `claude mcp add -s user` 写入）
    Codex         $CODEX_HOME/config.toml（默认 ~/.codex/config.toml）的
                  [mcp_servers.openi-bridge]，存在多份 home 时每份都写

令牌不写进配置，运行时从 ~/.openi/token.json（或 OPENI_TOKEN）读，所以配置里
不会出现明文密钥，换令牌也不用改配置。

用法：

    python -m openi_bridge.install_mcp            # 打印两边的配置片段和命令
    python -m openi_bridge.install_mcp --apply    # 直接写进两边的配置
    python -m openi_bridge.install_mcp --json     # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .client import dump

SERVER_NAME = "openi-bridge"
CLAUDE_CONFIG = Path.home() / ".claude.json"


def codex_config_paths() -> List[Path]:
    """Codex 的配置可能不止一份。

    CLI 读的是 ``$CODEX_HOME/config.toml``（没设就是 ``~/.codex/config.toml``），
    而桌面包和 TUI 可能各用一个 home。这里把所有存在（或那个默认的）都列出来，
    一起写，免得只配上一个、换个客户端就找不到工具。
    """
    paths: List[Path] = []
    home = os.environ.get("CODEX_HOME")
    if home:
        paths.append(Path(home).expanduser() / "config.toml")
    default = Path.home() / ".codex" / "config.toml"
    if default not in paths and (default.exists() or not paths):
        paths.append(default)
    return paths


def console_script() -> List[str]:
    """算出启动 MCP 服务的绝对命令（不带 shell 展开）。"""
    candidate = Path(sys.executable).parent / SERVER_NAME
    if candidate.exists():
        return [str(candidate)]
    # 没装 console script（例如直接 python -m 跑这个模块）时退回到模块形式。
    return [sys.executable, "-m", "openi_bridge.server"]


def codex_block(command: Sequence[str]) -> str:
    args = ", ".join(json.dumps(part) for part in command[1:])
    return (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f"command = {json.dumps(command[0])}\n"
        f"args = [{args}]\n"
    )


def claude_command(command: Sequence[str]) -> List[str]:
    return [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "--transport",
        "stdio",
        SERVER_NAME,
        *command,
    ]


def _upsert_toml_section(text: str, header: str, body: List[str]) -> Tuple[str, str]:
    """在 config.toml 里加/替换一个 section，其余内容原样保留。"""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        block = [header, *body]
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "\n".join(block) + "\n", "added"
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    merged = lines[:start] + [header, *body] + lines[end:]
    return "\n".join(merged).rstrip("\n") + "\n", "updated"


def apply_codex(command: Sequence[str], path: Path) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text() if path.exists() else ""
    args = ", ".join(json.dumps(part) for part in command[1:])
    body = [f"command = {json.dumps(command[0])}", f"args = [{args}]"]
    updated, action = _upsert_toml_section(original, f"[mcp_servers.{SERVER_NAME}]", body)
    if updated != original:
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text(updated)
    return {"path": str(path), "action": action}


def apply_all_codex(command: Sequence[str]) -> List[Dict[str, Any]]:
    return [apply_codex(command, path) for path in codex_config_paths()]


def apply_claude(command: Sequence[str]) -> Dict[str, Any]:
    cli = shutil.which("claude")
    if not cli:
        return {"action": "skipped", "reason": "没找到 claude 命令，请手动加配置文件"}
    # 先删掉同名旧条目，避免重复；删不到就忽略。
    subprocess.run(
        [cli, "mcp", "remove", "--scope", "user", SERVER_NAME],
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [cli, *claude_command(command)[1:]], capture_output=True, text=True
    )
    return {
        "action": "installed" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
    }


def status(command: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    command = list(command or console_script())
    claude_text = CLAUDE_CONFIG.read_text() if CLAUDE_CONFIG.exists() else ""
    try:
        claude_servers = json.loads(claude_text).get("mcpServers", {}) if claude_text else {}
    except json.JSONDecodeError:
        claude_servers = {}
    codex = []
    for path in codex_config_paths():
        text = path.read_text() if path.exists() else ""
        codex.append(
            {
                "path": str(path),
                "configured": f"[mcp_servers.{SERVER_NAME}]" in text,
            }
        )
    return {
        "command": command,
        "command_exists": Path(command[0]).exists(),
        "token_file": (Path.home() / ".openi" / "token.json").exists(),
        "codex": codex,
        "codex_configured": all(entry["configured"] for entry in codex) if codex else False,
        "claude_configured": SERVER_NAME in claude_servers,
        "claude_entry": claude_servers.get(SERVER_NAME),
    }


def plan(command: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    command = list(command or console_script())
    return {
        "ok": True,
        "server_name": SERVER_NAME,
        "command": command,
        "codex_configs": [str(path) for path in codex_config_paths()],
        "codex_block": codex_block(command),
        "claude_command": " ".join(claude_command(command)),
        "notes": [
            "配置里只放启动命令，不放令牌；运行时从 ~/.openi/token.json 读。",
            "启动命令是绝对路径，所以任何目录下的 Claude Code / Codex 都能拉起它。",
            "改完重启一次客户端（Claude Code 用 /mcp 查看，Codex 用 /mcp 或工具列表）。",
        ],
        "status": status(command),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m openi_bridge.install_mcp",
        description="把 openi-bridge 注册到 Claude Code 与 Codex。",
    )
    parser.add_argument("--apply", action="store_true", help="真的写进两边的配置")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    command = console_script()
    report: Dict[str, Any] = plan(command)
    if args.apply:
        report["applied"] = {
            "codex": apply_all_codex(command),
            "claude": apply_claude(command),
        }
        report["status"] = status(command)

    if args.json:
        print(dump(report))
        return 0

    state = report["status"]
    print(f"启动命令    {' '.join(command)}")
    print(
        "令牌文件    "
        + ("有" if state["token_file"] else "没有（先写 ~/.openi/token.json 或设 OPENI_TOKEN）")
    )
    for entry in state["codex"]:
        print(f"Codex       {'已配置' if entry['configured'] else '未配置'}  {entry['path']}")
    print(f"Claude Code {'已配置' if state['claude_configured'] else '未配置'}  {CLAUDE_CONFIG}")
    if "applied" in report:
        for entry in report["applied"]["codex"]:
            print(f"写入 Codex  {entry['action']}  {entry['path']}")
        claude_result = report["applied"]["claude"]
        detail = f"（{claude_result['stderr']}）" if claude_result.get("stderr") else ""
        print(f"写入 Claude {claude_result.get('action')}{detail}")
    if not args.apply:
        print()
        print("Codex 配置片段（追加到上面列出的 config.toml）：")
        print(codex_block(command))
        print("Claude Code 一条命令：")
        print(" ".join(claude_command(command)))
        print()
        print("加 --apply 就直接写进去。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
