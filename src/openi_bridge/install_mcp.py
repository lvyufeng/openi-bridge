"""把 openi-bridge 注册到 Claude Code / Codex / dsh / Cursor，任意目录都能直接调用。

MCP 服务的启动方式只有一条硬要求：客户端要能从一个**绝对路径**把它拉起来，
且它不依赖当前工作目录。这个模块负责把启动命令算成绝对路径，再写进各家的配置：

    Claude Code   ~/.claude.json 的 mcpServers（通过 `claude mcp add -s user` 写入）
    Codex         $CODEX_HOME/config.toml（默认 ~/.codex/config.toml）的
                  [mcp_servers.openi-bridge]，存在多份 home 时每份都写
    dsh           $DSH_HOME/profiles/<profile>/cordis.patch.yml，**每个 profile 一份**，
                  形状是顶层 YAML 数组里的 @deepseek-ai/dsh-mcp-client 条目
    Cursor        用户 profile（`cursor --add-mcp`），收的是一条**扁平**服务器定义，
                  必须带 name——和别家的 {"mcpServers": {...}} 嵌套形状不同

两种配置形状需要特别小心，别的地方踩过：

  * dsh 的补丁文件是人手维护的 YAML（全是注释和自定义插件），所以只做**行级插入**，
    绝不重新序列化——round-trip 一次注释就没了。它的默认模板是一行 `[]`，要替换掉
    而不是往后追加，追加会变成两个 YAML 文档、dsh 直接解析失败。
  * Cursor 拒绝嵌套形式（"Missing name property"），必须扁平 + name。

令牌不写进任何配置，运行时从 ~/.openi/token.json（或 OPENI_TOKEN）读，所以配置里
不会出现明文密钥，换令牌也不用改配置。测试断言了这一点。

用法：

    python -m openi_bridge.install_mcp            # 打印各家的配置片段和命令
    python -m openi_bridge.install_mcp --apply    # 直接写进各家配置
    python -m openi_bridge.install_mcp --json     # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 备份文件名用的时区；与 autopoint 一致。
BEIJING = timezone(timedelta(hours=8))

# 调 claude CLI 的超时。它可能等交互输入，不设上限会把安装卡死。
CLI_TIMEOUT_SECONDS = 60.0

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


def dsh_patch_paths() -> List[Path]:
    """dsh 的 MCP 服务器写在每个 profile 的补丁层里。

    dsh 把 profile 定义成"插件包 + 用户覆盖"的叠加，MCP 服务器是其中一类插件
    条目（``@deepseek-ai/dsh-mcp-client``）。用户层是
    ``$DSH_HOME/profiles/<name>/cordis.patch.yml``，所以每个 profile 各有一份：
    只写 tui 的话，web/headless 里就没有这个工具。
    """
    home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh")).expanduser()
    profiles = home / "profiles"
    if not profiles.is_dir():
        return []
    return sorted(profiles.glob("*/cordis.patch.yml"))


def dsh_entry(command: Sequence[str], indent: str = "    ") -> List[str]:
    """生成一条 dsh MCP 客户端条目（YAML，缩进与周围条目对齐）。"""
    i = indent
    args = ", ".join(json.dumps(part) for part in command[1:])
    return [
        f"{i}- id: mcp-{SERVER_NAME}",
        f"{i}  name: '@deepseek-ai/dsh-mcp-client'",
        f"{i}  config:",
        f"{i}    transport: stdio",
        f"{i}    serverName: {SERVER_NAME}",
        f"{i}    command: {command[0]}",
        f"{i}    args: [{args}]",
    ]


def apply_dsh(command: Sequence[str], path: Path) -> Dict[str, Any]:
    """把一个 dsh profile 的 MCP 条目加进去（幂等：已存在就替换）。

    只做行级的插入/替换，不解析整个 YAML——这是别人的配置文件，里面全是注释和
    自定义插件，round-trip 一次就会把注释和格式全洗掉。
    """
    original = path.read_text() if path.exists() else ""
    lines = original.splitlines()
    marker = f"- id: mcp-{SERVER_NAME}"
    start = next((i for i, l in enumerate(lines) if l.strip() == marker), None)

    if start is not None:
        # 找到这条 entry 的缩进，替换到同缩进的下一条 "- " 之前
        indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
        end = len(lines)
        for j in range(start + 1, len(lines)):
            l = lines[j]
            if not l.strip():
                continue
            if l.startswith(indent + "- "):
                end = j
                break
            if l and not l.startswith(" "):
                end = j
                break
        body = dsh_entry(command, indent)
        merged = lines[:start] + body + lines[end:]
        action = "updated"
    else:
        body = dsh_entry(command)
        # profile 的补丁层是一个**顶层 YAML 数组**：默认模板就是一行 `[]`。
        # 往 `[]` 后面接第二个文档会直接把文件写坏（dsh 报 "document separator
        # is expected"），所以先把空的 `[]` 换掉。
        empty_array = next(
            (i for i, l in enumerate(lines) if l.strip() == "[]"), None
        )
        idx = next((i for i, l in enumerate(lines) if l.rstrip() == "- insert:"), None)
        if empty_array is not None and idx is None:
            merged = lines[:empty_array] + ["- insert:", *body] + lines[empty_array + 1:]
        elif idx is None:
            merged = lines + ["", "- insert:", *body]
        else:
            # 插到该 insert 列表已有条目的末尾
            j = idx + 1
            while j < len(lines):
                l = lines[j]
                if l.strip() and not l.startswith("    "):
                    break
                j += 1
            merged = lines[:j] + body + lines[j:]
        action = "added"

    updated = "\n".join(merged).rstrip("\n") + "\n"
    if updated == original:
        return {"path": str(path), "action": "unchanged"}
    backup = None
    if path.exists():
        stamp = datetime.now(BEIJING).strftime("%Y%m%d%H%M%S")
        backup = path.with_suffix(path.suffix + f".{stamp}.bak")
        shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(updated)
    os.replace(tmp, path)
    return {"path": str(path), "action": action, "backup": str(backup) if backup else None}


def apply_all_dsh(command: Sequence[str]) -> List[Dict[str, Any]]:
    return [apply_dsh(command, path) for path in dsh_patch_paths()]


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


def cursor_payload(command: Sequence[str]) -> str:
    """Cursor 的 --add-mcp 收的是**一条服务器定义**（扁平对象，含 name）。

    注意它和 Claude/Codex 的配置形状不同，也不是 {"mcpServers": {...}}：实测传
    嵌套形式会被拒（"Missing name property"），平铺并带 name 才认。
    和别处一样只放启动命令，不放令牌。
    """
    return json.dumps(
        {"name": SERVER_NAME, "command": command[0], "args": list(command[1:])},
        ensure_ascii=False,
    )


def apply_cursor(command: Sequence[str]) -> Dict[str, Any]:
    """通过 ``cursor --add-mcp`` 把服务写进用户 profile。"""
    cli = shutil.which("cursor")
    if not cli:
        return {"action": "skipped", "reason": "没找到 cursor 命令，请手动加配置"}
    try:
        result = subprocess.run(
            [cli, "--add-mcp", cursor_payload(command)],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "action": "failed",
            "reason": f"cursor --add-mcp 超过 {CLI_TIMEOUT_SECONDS}s 没有返回，已放弃",
        }
    return {
        "action": "installed" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
    }


def apply_codex(command: Sequence[str], path: Path) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text() if path.exists() else ""
    args = ", ".join(json.dumps(part) for part in command[1:])
    body = [f"command = {json.dumps(command[0])}", f"args = [{args}]"]
    updated, action = _upsert_toml_section(original, f"[mcp_servers.{SERVER_NAME}]", body)
    backup = None
    if updated != original:
        if path.exists():
            # 备份带时间戳，而不是每次覆盖同一个 .bak：连跑两次写坏了的配置时，
            # 唯一的 .bak 已经被上一次覆盖，原始内容就找不回来了。
            stamp = datetime.now(BEIJING).strftime("%Y%m%d%H%M%S")
            backup = path.with_suffix(path.suffix + f".{stamp}.bak")
            shutil.copy2(path, backup)
        # 先写临时文件再 os.replace：write_text 是就地截断，写到一半失败（磁盘满、
        # 进程被杀）会留下一个被截断的 config.toml，客户端下次启动就崩。
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(updated)
        os.replace(tmp, path)
    return {"path": str(path), "action": action, "backup": str(backup) if backup else None}


def apply_all_codex(command: Sequence[str]) -> List[Dict[str, Any]]:
    return [apply_codex(command, path) for path in codex_config_paths()]


def apply_claude(command: Sequence[str]) -> Dict[str, Any]:
    cli = shutil.which("claude")
    if not cli:
        return {"action": "skipped", "reason": "没找到 claude 命令，请手动加配置文件"}
    # 先删掉同名旧条目，避免重复；删不到就忽略。两条命令都设超时：claude CLI 在
    # 某些状态下会等交互输入，不设超时会把整个安装卡死在这里。
    removed = subprocess.run(
        [cli, "mcp", "remove", "--scope", "user", SERVER_NAME],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
    )
    try:
        result = subprocess.run(
            [cli, *claude_command(command)[1:]],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "action": "failed",
            "reason": f"claude mcp add 超过 {CLI_TIMEOUT_SECONDS}s 没有返回，已放弃",
            "remove_returncode": removed.returncode,
        }
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
    dsh = []
    for path in dsh_patch_paths():
        text = path.read_text() if path.exists() else ""
        dsh.append(
            {
                "path": str(path),
                "profile": path.parent.name,
                "configured": f"- id: mcp-{SERVER_NAME}" in text,
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
        "dsh": dsh,
        "dsh_configured": all(entry["configured"] for entry in dsh) if dsh else False,
    }


def plan(command: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    command = list(command or console_script())
    return {
        "ok": True,
        "server_name": SERVER_NAME,
        "command": command,
        "codex_configs": [str(path) for path in codex_config_paths()],
        "codex_block": codex_block(command),
        "dsh_profiles": [str(path) for path in dsh_patch_paths()],
        "dsh_block": "\n".join(["- insert:", *dsh_entry(command)]),
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
        description="把 openi-bridge 注册到 Claude Code、Codex、dsh 与 Cursor。",
    )
    parser.add_argument("--apply", action="store_true", help="真的写进各客户端的配置")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    command = console_script()
    report: Dict[str, Any] = plan(command)
    if args.apply:
        report["applied"] = {
            "codex": apply_all_codex(command),
            "claude": apply_claude(command),
            "dsh": apply_all_dsh(command),
            "cursor": apply_cursor(command),
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
    if state["dsh"]:
        for entry in state["dsh"]:
            print(
                f"dsh         {'已配置' if entry['configured'] else '未配置'}"
                f"  [{entry['profile']}] {entry['path']}"
            )
    else:
        print("dsh         未发现 profile（$DSH_HOME/profiles/*/cordis.patch.yml）")
    if "applied" in report:
        for entry in report["applied"]["codex"]:
            print(f"写入 Codex  {entry['action']}  {entry['path']}")
        claude_result = report["applied"]["claude"]
        detail = f"（{claude_result['stderr']}）" if claude_result.get("stderr") else ""
        print(f"写入 Claude {claude_result.get('action')}{detail}")
        for entry in report["applied"]["dsh"]:
            print(f"写入 dsh    {entry['action']}  {entry['path']}")
        cur = report["applied"]["cursor"]
        cur_detail = f"（{cur.get('stderr') or cur.get('reason') or ''}）" if (cur.get("stderr") or cur.get("reason")) else ""
        print(f"写入 Cursor {cur.get('action')}{cur_detail}")
    if not args.apply:
        print()
        print("Codex 配置片段（追加到上面列出的 config.toml）：")
        print(codex_block(command))
        print("Claude Code 一条命令：")
        print(" ".join(claude_command(command)))
        print()
        print("dsh 配置片段（追加到上面列出的 cordis.patch.yml）：")
        print(report["dsh_block"])
        print()
        print("Cursor 一条命令：")
        print(f"cursor --add-mcp '{cursor_payload(command)}'")
        print()
        print("加 --apply 就直接写进去。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
