"""MCP server exposing OpenI (启智社区) compute as tools.

Run with:  OPENI_TOKEN=... python -m openi_bridge.server
or through the console script:  openi-bridge
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .client import (
    DEFAULT_CLUSTER,
    DEFAULT_IMAGE_URL,
    OpenIClient,
    OpenIError,
    dump,
    summarize_spec,
)
from .resources import COMPUTE_SOURCES, describe_resources

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server


mcp = _Server(
    "openi-bridge",
    instructions=(
        "使用启智社区（openi.pcl.ac.cn，即 OpenI 鹏城云脑普惠算力）的计算任务。"
        "可以查询账号、算力资源与积分、可用规格（spec）、代码仓、任务的日志与状态，"
        "并新建、停止、重启、删除计算任务。创建任务前先用 "
        "openi_list_creation_options 拿到实时的 spec_id 与镜像。使用前必须设置 "
        "OPENI_TOKEN 环境变量，令牌在 https://openi.pcl.ac.cn/user/settings/applications 生成。"
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
def openi_list_creation_options(
    job_type: str = "DEBUG",
    compute_source: str = "GPU",
    cluster_type: str = DEFAULT_CLUSTER,
    visualize_required: bool = False,
    repo: Optional[str] = None,
) -> str:
    """列出当前账号在这个作业类型/算力类型下真正可开的规格与镜像。

    返回每一项的 spec_id、卡型、卡数、CPU/内存、单价，以及可用镜像的
    image_id/image_name、默认任务名、积分余额和是否还能创建任务。
    创建任务时必须用这里返回的 spec_id。

    job_type: DEBUG / TRAIN / INFERENCE / ONLINEINFERENCE / HPC 等。
    compute_source: GPU / NPU / GCU / MLU / DCU / CPU / ILUVATAR-GPGPU / METAX-GPGPU / BIREN-GPU。
    cluster_type: 默认 C2Net（普通用户真实可用的集群）。
    """
    try:
        with _client() as client:
            info = client.creation_info(
                job_type=job_type,
                compute_source=compute_source,
                cluster_type=cluster_type,
                visualize_required=visualize_required,
                repo=repo,
            )
        specs = ((info.get("specs") or {}).get("all")) or []
        point = info.get("point_account") or {}
        result = {
            "ok": True,
            "job_type": job_type,
            "compute_source": compute_source,
            "cluster_type": cluster_type,
            "can_create_more": info.get("can_create_more"),
            "not_stop_task_count": info.get("not_stop_task_count"),
            "limit_count": info.get("limit_count"),
            "waiting_queue_count": info.get("wait_count"),
            "default_display_job_name": info.get("display_job_name"),
            "point_balance": point.get("balance"),
            "allowed_worker_num": info.get("allowed_worker_num"),
            "can_use_all_images": info.get("can_use_all_images"),
            "images": info.get("images") or [],
            "images_usable": False,
            "images_note": (
                "上面 images 是平台登记的默认镜像，但在 C2Net 上会以 "
                "'image not available' 失败。请用 openi_list_public_images 拿到可用"
                "镜像地址并作为 image_url 传给 openi_start_task；不传镜像时会自动"
                "使用默认镜像。"
            ),
            "specs": [summarize_spec(s) for s in specs],
        }
        return dump(result)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_estimate_runtime(
    compute_sources: Optional[list[str]] = None,
    job_type: str = "DEBUG",
    cluster_type: str = DEFAULT_CLUSTER,
) -> str:
    """用当前积分余额估算每个规格还能开多少机时（小时/天）。

    计费规则：消耗积分 = 单价（分/卡时）× 卡数 × 小时数，每 6 分钟结算一次，
    不足 6 分钟不计费。所以机时按 0.1 小时的整数倍向下取整。单价比对的是
    openi_list_creation_options 里的实时 unit_price，不是离线目录。

    compute_sources: 要估算的算力类型，默认覆盖全部（GPU / NPU / GCU / MLU /
        DCU / ILUVATAR-GPGPU / METAX-GPGPU / BIREN-GPU）。
    """
    sources = compute_sources or list(COMPUTE_SOURCES)
    try:
        balance: Optional[float] = None
        rows: list[dict[str, Any]] = []
        for source in sources:
            with _client() as client:
                info = client.creation_info(job_type=job_type, compute_source=source, cluster_type=cluster_type)
            if balance is None:
                balance = ((info.get("point_account") or {}).get("balance"))
            for spec in ((info.get("specs") or {}).get("all")) or []:
                slim = summarize_spec(spec)
                price = slim.get("price_per_card_hour")
                row = {
                    "compute_source": source,
                    "spec_id": slim.get("spec_id"),
                    "card_type": slim.get("card_type"),
                    "card_count": slim.get("card_count"),
                    "price_per_hour": price,
                }
                if balance is not None and price:
                    blocks = int(balance // (price * 0.1))  # 每块 6 分钟
                    hours = round(blocks * 0.1, 1)
                    row["hours"] = hours
                    row["days"] = round(hours / 24, 1)
                    row["blocks_of_6min"] = blocks
                rows.append(row)
        return dump(
            {
                "ok": True,
                "balance": balance,
                "job_type": job_type,
                "cluster_type": cluster_type,
                "billing": "积分 = 单价(分/卡时) × 卡数 × 小时；每 6 分钟结算，不足 6 分钟不计费。",
                "note": "hours/days 已按 6 分钟向下取整；CPU 没有可用规格所以不在列表里。",
                "estimates": rows,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_images_for_spec(
    spec_id: int,
    job_type: str = "DEBUG",
    compute_source: str = "GPU",
    cluster_type: str = DEFAULT_CLUSTER,
    has_internet: int = 2,
    visualize_required: bool = False,
) -> str:
    """查询某个 spec 可用的镜像。

    在 C2Net 上这个接口通常返回空，说明该规格没有绑定镜像，需要用
    openi_list_public_images 里的镜像地址（place）以 image_url 传入。
    """
    try:
        with _client() as client:
            images = client.images_for_spec(
                spec_id,
                job_type=job_type,
                compute_source=compute_source,
                cluster_type=cluster_type,
                has_internet=has_internet,
                visualize_required=visualize_required,
            )
        return dump({"ok": True, "images": images})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_public_images() -> str:
    """列出可以真正拉起的公共镜像地址（place）。

    返回平台推荐镜像与本账号收藏的镜像。用其中的 place 作为
    openi_start_task 的 image_url；不传镜像时工具会用默认镜像
    （可用环境变量 OPENI_DEFAULT_IMAGE_URL 覆盖）。
    """
    try:
        with _client() as client:
            recommended = client.recommended_images()
            starred = client.starred_images()

        def slim(payload: Any) -> list:
            images = payload.get("images") if isinstance(payload, dict) else payload
            out = []
            for image in images or []:
                if not isinstance(image, dict):
                    continue
                out.append(
                    {
                        "tag": image.get("tag"),
                        "image_url": image.get("place"),
                        "compute_resource": image.get("compute_resource"),
                        "train_type": image.get("trainType"),
                        "framework": image.get("framework"),
                        "description": (image.get("description") or "").strip()[:160],
                        "use_count": image.get("useCount"),
                    }
                )
            return out

        return dump(
            {
                "ok": True,
                "default_image_url": DEFAULT_IMAGE_URL,
                "recommended": slim(recommended),
                "starred": slim(starred),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


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
    """列出代码仓根目录与分支，用于确定启动文件 boot_file（例如 train.py）。"""
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
def openi_list_tasks(page: int = 1, page_size: int = 20) -> str:
    """列出当前账号的全部计算任务（跨所有代码仓）。"""
    try:
        with _client() as client:
            data = client.list_tasks(page=page, page_size=page_size)
        if not isinstance(data, dict):
            return dump({"ok": True, "tasks": data})
        slim = []
        for item in data.get("tasks", []):
            task = item.get("task", item) if isinstance(item, dict) else {}
            if not isinstance(task, dict):
                continue
            slim.append(
                {
                    "id": task.get("id"),
                    "display_job_name": task.get("display_job_name"),
                    "job_type": task.get("job_type"),
                    "status": task.get("status"),
                    "compute_source": task.get("compute_source"),
                    "card_type": task.get("acc_card_type"),
                    "cluster": task.get("cluster"),
                    "created_unix": task.get("created_unix"),
                    "duration": task.get("formatted_duration"),
                    "repo": item.get("repo_alias") if isinstance(item, dict) else None,
                }
            )
        return dump(
            {
                "ok": True,
                "total": data.get("total"),
                "page": data.get("page"),
                "page_size": data.get("page_size"),
                "can_create_task": data.get("can_create_task"),
                "tasks": slim,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_repo_tasks(repo: str) -> str:
    """列出某个代码仓（owner/name）下的全部计算任务。

    优先用仓级接口 /{owner}/{repo}/ai_task；该接口对部分令牌会返回 403，
    此时退回到账号级 /ai_task/my_list 再按仓名过滤，结果里会带 fallback 标记。
    """
    try:
        with _client() as client:
            try:
                tasks = client.repo_tasks(repo)
                return dump({"ok": True, "repo": repo, "tasks": tasks})
            except OpenIError as exc:
                if exc.status_code != 403:
                    raise
                name = repo.split("/")[-1]
                data = client.list_tasks(page=1, page_size=100)
                matched = [
                    item
                    for item in (data.get("tasks") or [])
                    if isinstance(item, dict)
                    and (item.get("repo_alias") in (name, repo) or item.get("repo_name") in (name, repo))
                ]
                return dump(
                    {
                        "ok": True,
                        "repo": repo,
                        "fallback": "repo 级接口返回 403，已用账号级任务列表按仓名过滤",
                        "tasks": matched,
                    }
                )
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
    spec_id: int,
    job_type: str = "DEBUG",
    compute_source: str = "GPU",
    cluster: str = DEFAULT_CLUSTER,
    display_job_name: Optional[str] = None,
    work_server_number: int = 1,
    branch_name: Optional[str] = None,
    image_id: Optional[str] = None,
    image_name: Optional[str] = None,
    image_url: Optional[str] = None,
    boot_file: Optional[str] = None,
    params: Optional[str] = None,
    dataset_uuid_str: Optional[str] = None,
    pretrain_model_id_str: Optional[str] = None,
    description: Optional[str] = None,
    has_internet: int = 2,
    time_limit: Optional[int] = None,
    visualize_required: bool = False,
    extra_fields: Optional[str] = None,
) -> str:
    """新建并启动一个计算任务，成功后立即开始计费（不足 6 分钟不扣费）。

    先用 openi_list_creation_options 获取实时 spec_id 与镜像，再用这里创建。

    repo: 代码仓 owner/name，任务从该仓的代码运行。
    spec_id: 必填，规格 ID，来自 openi_list_creation_options。
    job_type: DEBUG（交互调试，可开 Jupyter/SSH）/ TRAIN（训练）/ INFERENCE（批量推理）等。
    compute_source: GPU / NPU / GCU / MLU / DCU / CPU / ILUVATAR-GPGPU / METAX-GPGPU / BIREN-GPU。
    cluster: 默认 C2Net（普通用户真实可用的集群）。
    display_job_name: 任务名，留空用平台默认名（必须是小写字母数字，3-36 位）。
    work_server_number: 节点数，默认 1。
    branch_name: 代码分支，留空用仓库默认分支。
    image_id/image_name: 平台登记的镜像 ID，来自 openi_list_creation_options。留空时优先用实测跑通的镜像（按 spec_id 记录），没有则用平台列出的第一个；NVIDIA GPU 例外，它的登记镜像在 C2Net 上拉不起来，会自动改用 image_url。
    image_url: 镜像地址（place）。留空时按上面的规则自动选择，地址见 openi_list_public_images。
    boot_file: 启动文件（训练任务需要，例如 train.py）。
    params: 传给启动文件的运行参数。
    dataset_uuid_str / pretrain_model_id_str: 挂载的数据集 UUID / 预训练模型 ID。
    has_internet: 0 不限制 / 1 无外网 / 2 有外网。
    time_limit: 自动停止时长（小时，-1 表示不自动停止）。
    visualize_required: 是否需要 TensorBoard。
    extra_fields: JSON 字符串，用于补充平台新增字段。
    """
    try:
        extra = json.loads(extra_fields) if extra_fields else None
        with _client() as client:
            result = client.create_task(
                repo=repo,
                spec_id=spec_id,
                display_job_name=display_job_name,
                compute_source=compute_source,
                cluster=cluster,
                job_type=job_type,
                work_server_number=work_server_number,
                branch_name=branch_name,
                image_id=image_id,
                image_name=image_name,
                image_url=image_url,
                boot_file=boot_file,
                params=params,
                dataset_uuid_str=dataset_uuid_str,
                pretrain_model_id_str=pretrain_model_id_str,
                description=description,
                has_internet=has_internet,
                time_limit=time_limit,
                visualize_required=visualize_required,
                extra=extra,
            )
        return _ok(result, ok=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_start_task_raw(repo: str, payload_json: str, dry_run: bool = False) -> str:
    """用完全自定义的 JSON 请求体创建任务，用于平台接口字段变化时兜底。

    payload_json 直接作为请求体发送到 /{repo}/ai_task/create。
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
def openi_delete_task(task_id: str) -> str:
    """删除任务（不可恢复）。任务需先处于已停止状态。"""
    try:
        with _client() as client:
            return _ok(client.task_action("del", task_id), ok=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_sdk_code(job_type: str = "DEBUG", compute_source: str = "GPU", visualize: bool = False) -> str:
    """生成任务容器内访问数据集/模型的示例代码。"""
    try:
        with _client() as client:
            return dump(
                {
                    "ok": True,
                    "sdk_code": client.sdk_code(
                        job_type=job_type,
                        compute_source=compute_source,
                        visualize_required=visualize,
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
