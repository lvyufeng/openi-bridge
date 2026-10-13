"""MCP server exposing OpenI (启智社区) compute as tools.

Run with:  OPENI_TOKEN=... python -m openi_bridge.server
or through the console script:  openi-bridge
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .client import OpenIClient, OpenIError, dump
from .resources import CLUSTERS, JOB_TYPES, describe_resources

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server


mcp = _Server(
    "openi-bridge",
    instructions=(
        "使用启智社区（openi.pcl.ac.cn，即 OpenI 鹏城云脑普惠算力）的计算任务。"
        "可以查询账号、算力资源与积分、代码仓、任务的日志与状态，并新建、停止、"
        "重启、删除计算任务。使用前必须设置 OPENI_TOKEN 环境变量，令牌在 "
        "https://openi.pcl.ac.cn/user/settings/applications 生成。"
    ),
)


def _client() -> OpenIClient:
    return OpenIClient()


def _err(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2)


def _ok(value: Any, **extra: Any) -> str:
    if isinstance(value, dict):
        merged = dict(value)
        merged.update(extra)
        return dump(merged)
    return dump({"result": value, **extra})


@mcp.tool()
def openi_whoami() -> str:
    """查看当前令牌对应的启智账号，用来确认令牌是否可用。"""
    try:
        with _client() as client:
            return _ok(client.current_user(), ok=True)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
        return _err(exc)


@mcp.tool()
def openi_list_resources() -> str:
    """列出启智社区可用的算力集群、卡型与积分单价（分/卡时）。"""
    return describe_resources()


@mcp.tool()
def openi_list_my_repositories(limit: int = 50) -> str:
    """列出当前账号的代码仓。新建计算任务时需要指定 owner/name 代码仓。"""
    try:
        with _client() as client:
            repos = client.my_repos(limit=limit)
        slim = [
            {
                "full_name": r.get("full_name"),
                "private": r.get("private"),
                "empty": r.get("empty"),
                "updated_at": r.get("updated_at"),
                "description": r.get("description"),
            }
            for r in repos
            if isinstance(r, dict)
        ]
        return dump({"ok": True, "count": len(slim), "repositories": slim})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_search_repositories(query: str, limit: int = 20) -> str:
    """按关键字搜索启智社区上的公开代码仓。"""
    try:
        with _client() as client:
            result = client.search_repos(query, limit=limit)
        repos = result.get("data", []) if isinstance(result, dict) else result
        slim = [
            {
                "full_name": r.get("full_name"),
                "description": r.get("description"),
                "updated_at": r.get("updated_at"),
            }
            for r in repos
            if isinstance(r, dict)
        ]
        return dump({"ok": True, "count": len(slim), "repositories": slim})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_repo_files(repo: str, ref: Optional[str] = None) -> str:
    """列出代码仓根目录文件，用于确定启动文件 boot_file（例如 train.py）。"""
    try:
        with _client() as client:
            files = client.repo_contents(repo, ref=ref)
            branches = client.repo_branches(repo)
        names = [f.get("name") for f in files] if isinstance(files, list) else files
        branch_names = [b.get("name") for b in branches] if isinstance(branches, list) else branches
        return dump({"ok": True, "files": names, "branches": branch_names})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_get_task(task_id: str) -> str:
    """按任务 ID 查询任务详情（状态、资源、时长等）。"""
    try:
        with _client() as client:
            detail = client.task(task_id)
            if not isinstance(detail, dict):
                return _ok(detail, ok=True)
            try:
                detail.update(client.task_brief(task_id) or {})
            except OpenIError:
                pass
        return _ok(detail, ok=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_tasks(repo: str) -> str:
    """列出某个代码仓（owner/name）下的全部计算任务。"""
    try:
        with _client() as client:
            tasks = client.list_tasks(repo)
        return dump({"ok": True, "repo": repo, "tasks": tasks})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_logs(task_id: str, repo: Optional[str] = None, lines: int = 200, order: str = "desc") -> str:
    """读取计算任务的运行日志，默认最近 200 行。"""
    try:
        with _client() as client:
            logs = client.task_log(task_id, lines=lines, order=order, repo=repo)
        return dump({"ok": True, "logs": logs})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_resource_usage(task_id: str) -> str:
    """查看任务当前的算力占用（卡数、CPU、内存、耗时）。"""
    try:
        with _client() as client:
            usage = client.task_resource_usage(task_id)
        return dump({"ok": True, "resource_usage": usage})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_debug_url(task_id: str, repo: Optional[str] = None) -> str:
    """获取调试任务的 Jupyter / SSH 访问入口。"""
    try:
        with _client() as client:
            return dump({"ok": True, "debug": client.debug_url(task_id, repo=repo)})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_visualize_url(task_id: str) -> str:
    """获取训练任务的 TensorBoard 可视化入口。"""
    try:
        with _client() as client:
            return dump({"ok": True, "visualize": client.visualize_url(task_id)})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_start_task(
    repo: str,
    task_name: str,
    job_type: str = "DEBUG",
    cluster: str = "openi",
    card_type: str = "V100",
    card_count: int = 1,
    node_count: int = 1,
    image: Optional[str] = None,
    boot_file: Optional[str] = None,
    branch: Optional[str] = None,
    dataset: Optional[str] = None,
    model: Optional[str] = None,
    command: Optional[str] = None,
    duration_hours: Optional[int] = None,
    description: Optional[str] = None,
    has_internet: Optional[bool] = None,
    visualize: Optional[bool] = None,
    extra_fields: Optional[str] = None,
) -> str:
    """新建并启动一个计算任务（调试/训练/推理），成功后会立刻开始计费。

    repo: 代码仓 owner/name，任务从该仓的代码运行。
    job_type: DEBUG（交互调试）/ TRAIN（训练）/ INFERENCE（推理）等。
    cluster: openi（启智集群）或 c2net（智算网络）。
    card_type/card_count/node_count: 卡型、每节点卡数、节点数。
    image: 容器镜像地址，留空则用平台默认镜像。
    boot_file: 启动文件，例如 train.py。
    command: 运行参数。
    duration_hours: 自动停止时长（小时）。
    extra_fields: JSON 字符串，用于覆盖或补充请求字段。
    """
    try:
        extra = json.loads(extra_fields) if extra_fields else None
        with _client() as client:
            result = client.create_task(
                repo=repo,
                task_name=task_name,
                job_type=job_type,
                cluster=cluster,
                card_type=card_type,
                card_count=card_count,
                node_count=node_count,
                image=image,
                boot_file=boot_file,
                branch=branch,
                dataset=dataset,
                model=model,
                command=command,
                duration_hours=duration_hours,
                description=description,
                has_internet=has_internet,
                visualize=visualize,
                extra=extra,
            )
        return _ok(result, ok=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_start_task_raw(repo: str, payload_json: str, dry_run: bool = False) -> str:
    """用完全自定义的 JSON 请求体创建任务，用于平台接口字段变化时兜底。

    例如 payload_json 可以是网页创建表单提交的原始字段。
    dry_run=True 时只回显将要发送的请求，不真正创建。
    """
    try:
        payload = json.loads(payload_json)
        if dry_run:
            return dump({"ok": True, "dry_run": True, "repo": repo, "payload": payload})
        with _client() as client:
            result = client.post_json(f"/{repo}/ai_task/create", json_body=payload)
        return _ok(result, ok=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_stop_task(task_id: str) -> str:
    """停止任务并释放算力（停止后立即停止计费）。"""
    try:
        with _client() as client:
            return _ok(client.task_action("stop", task_id), ok=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_restart_task(task_id: str) -> str:
    """重启一个已停止的任务。"""
    try:
        with _client() as client:
            return _ok(client.task_action("restart", task_id), ok=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_delete_task(task_id: str, repo: str) -> str:
    """删除任务（不可恢复）。"""
    try:
        with _client() as client:
            return _ok(client.task_action("del", task_id, repo=repo), ok=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_sdk_code(job_type: str = "DEBUG", cluster: str = "openi", visualize: bool = False) -> str:
    """生成任务容器内访问数据集/模型的示例代码。"""
    try:
        with _client() as client:
            return dump(
                {
                    "ok": True,
                    "sdk_code": client.sdk_code(
                        job_type=job_type, cluster_type=cluster, visualize_required=visualize
                    ),
                }
            )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def main() -> None:
    """Entry point used by the openi-bridge console script."""
    if not os.getenv("OPENI_TOKEN") and not os.path.exists(os.path.expanduser("~/.openi/token.json")):
        print(
            "提示：未检测到 OPENI_TOKEN，工具调用会返回 NO_TOKEN 错误。"
            "请在 https://openi.pcl.ac.cn/user/settings/applications 生成令牌并设置 OPENI_TOKEN。",
        )
    mcp.run("stdio")


if __name__ == "__main__":
    main()
