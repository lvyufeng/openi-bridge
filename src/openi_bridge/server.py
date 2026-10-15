"""MCP server exposing OpenI (启智社区) compute as tools.

Run with:  OPENI_TOKEN=... python -m openi_bridge.server
or through the console script:  openi-bridge
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

from .client import (
    DATASET_CATEGORIES,
    DATASET_LICENSES,
    DATASET_TASKS,
    DEFAULT_CLUSTER,
    DEFAULT_IMAGE_URL,
    OpenIClient,
    OpenIError,
    dump,
    expand_upload_paths,
    summarize_spec,
)
from . import autopoint, exec_on_card
from .resources import COMPUTE_SOURCES, POINT_ACTIONS, POINT_NOTES, describe_resources

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server


mcp = _Server(
    "openi-bridge",
    instructions=(
        "使用启智社区（openi.pcl.ac.cn，即 OpenI 鹏城云脑普惠算力）的计算任务。"
        "可以查询账号、算力资源与积分、可用规格（spec）、代码仓、任务的日志与状态，"
        "并新建、停止、重启、删除计算任务；也可以创建数据集/模型库并把本地文件"
        "（数据集、ckpt 权重等）上传上去，再把得到的 UUID 挂到计算任务上。创建任务前先用 "
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
def openi_points() -> str:
    """查看算力积分：余额、单日获取上限，以及平台公布的完整获取规则。

    规则来自 /reward/point/rule/config，余额来自 /reward/point/account。
    RefreshRate=DAILY 的按天重置，NOT_CYCLE 的是一次性奖励；打榜/活动奖励
    另行计算，不占这里的单日上限。
    """
    try:
        with _client() as client:
            account = client.points_account()
            config = client.points_rules()
        rules = config.get("TaskRules") or []
        enriched = []
        daily_max = 0.0
        for rule in rules:
            code = rule.get("TaskCode")
            amount = float(rule.get("AwardAmount") or 0)
            limit = float(rule.get("LimitNum") or 0)
            daily = rule.get("RefreshRate") == "DAILY"
            if daily:
                daily_max += amount * limit
            enriched.append(
                {
                    "action": POINT_ACTIONS.get(code, code),
                    "task_code": code,
                    "points": amount,
                    "cycle": "每日" if daily else "一次性",
                    "limit": int(limit),
                    "points_per_cycle": amount * limit,
                }
            )
        enriched.sort(key=lambda row: (-row["points"], row["action"]))
        daily_limit = float(config.get("UserDailyLimit") or 0)
        return dump(
            {
                "ok": True,
                "balance": account.get("Balance"),
                "total_earned": account.get("TotalEarned"),
                "total_consumed": account.get("TotalConsumed"),
                "user_daily_limit": daily_limit,
                "daily_rules_total_if_maxed": daily_max,
                "daily_ceiling": min(daily_max, daily_limit) if daily_limit else daily_max,
                "rules": enriched,
                "notes": POINT_NOTES,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_points_history(operate: str = "INCREASE", pages: int = 1) -> str:
    """查看积分流水，10 条一页。

    operate=INCREASE 看获取明细，DECREASE 看消耗明细。pages 最多翻多少页
    （每页 10 条），返回里会按天汇总，方便看每天实际拿了多少分。
    """
    try:
        pages = max(1, int(pages))
        records: list[dict[str, Any]] = []
        total = None
        with _client() as client:
            for page in range(1, pages + 1):
                data = client.points_records(operate=operate, page=page)
                total = data.get("Total", total)
                batch = data.get("Records") or []
                if not batch:
                    break
                records.extend(batch)
        per_day: dict[str, float] = {}
        per_source: dict[str, dict[str, float]] = {}
        rows = []
        for rec in records:
            amount = float(rec.get("Amount") or 0)
            ts = rec.get("LastOperateDate")
            day = None
            if ts:
                day = autopoint.beijing_day(ts)
                per_day[day] = per_day.get(day, 0.0) + amount
            source = rec.get("SourceTemplateId") or rec.get("SourceType") or "(其他)"
            entry = per_source.setdefault(source, {"count": 0.0, "points": 0.0})
            entry["count"] += 1
            entry["points"] += amount
            rows.append(
                {
                    "time": day,
                    "action": POINT_ACTIONS.get(source, source),
                    "points": amount,
                    "serial_no": rec.get("SerialNo"),
                    "remark": rec.get("Remark") or None,
                }
            )
        return dump(
            {
                "ok": True,
                "operate": operate.upper(),
                "total_records": total,
                "fetched": len(rows),
                "per_action": per_source,
                "per_day": dict(sorted(per_day.items(), reverse=True)),
                "records": rows,
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
def openi_create_dataset(
    name: str,
    alias: Optional[str] = None,
    is_private: bool = False,
    categories: Optional[list[str]] = None,
    tasks: Optional[list[str]] = None,
    licenses: str = "MIT",
) -> str:
    """新建一个空数据集，随后用 openi_upload_dataset_files 往里面传文件。

    数据集全名是 当前账号登录名/name，例如 scratch-repo。返回里的 id
    就是这个数据集的 UUID，可直接作为 openi_start_task 的 dataset_uuid_str。

    name: 数据集短名（URL 标识），只能字母/数字/连字符/下划线/点，字母数字开头结尾。
    alias: 中文显示名，可留空；只能中文/字母/数字/连字符/下划线/点。
    is_private: True 为私有数据集，默认公开。
    categories: 数据集类别，取自 DATASET_CATEGORIES，例如 ["natural_language_processing"]。
    tasks: 关联任务，例如 ["language_modeling"]；可用值见 DATASET_TASKS。
    licenses: 许可证，默认 MIT，可选值见 DATASET_LICENSES。
    """
    try:
        bad_cat = [c for c in (categories or []) if c not in DATASET_CATEGORIES]
        bad_task = [t for t in (tasks or []) if t not in DATASET_TASKS]
        if bad_cat:
            return dump({"ok": False, "error": f"未知的数据集类别：{bad_cat}", "allowed": list(DATASET_CATEGORIES)})
        if bad_task:
            return dump({"ok": False, "error": f"未知的数据集任务：{bad_task}", "allowed": list(DATASET_TASKS)})
        if licenses not in DATASET_LICENSES:
            return dump({"ok": False, "error": f"未知的许可证：{licenses}", "allowed": list(DATASET_LICENSES)})
        with _client() as client:
            result = client.create_dataset(
                name=name,
                alias=alias,
                is_private=is_private,
                categories=categories,
                tasks=tasks,
                licenses=licenses,
            )
        return dump(
            {
                "ok": True,
                "id": result.get("id"),
                "name": result.get("name"),
                "owner_name": result.get("owner_name"),
                "full_name": f"{result.get('owner_name')}/{result.get('name')}",
                "note": "把 id 作为 dataset_uuid_str 传给 openi_start_task 即可挂载。",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_create_model(
    name: str,
    alias: Optional[str] = None,
    is_private: bool = False,
    engine: int = 0,
    label: str = "",
    license: str = "",
) -> str:
    """新建一个空模型库，随后用 openi_upload_model_files 上传 ckpt 等权重文件。

    模型全名是 当前账号登录名/name。返回里的 id 即模型 ID，可直接作为
    openi_start_task 的 pretrain_model_id_str。

    name: 模型短名（URL 标识），规则同数据集。
    alias: 中文显示名，可留空。
    is_private: True 为私有模型，默认公开。
    engine: 引擎代号，默认 0（不指定）。
    label / license: 展示用标签与许可证，可留空。
    """
    try:
        with _client() as client:
            result = client.create_model(
                name=name,
                alias=alias,
                is_private=is_private,
                engine=engine,
                label=label,
                license=license,
            )
        return dump(
            {
                "ok": True,
                "id": result.get("id"),
                "name": result.get("name"),
                "owner_name": result.get("owner_name"),
                "full_name": f"{result.get('owner_name')}/{result.get('name')}",
                "note": "把 id 作为 pretrain_model_id_str 传给 openi_start_task 即可挂载。",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_data_subject_info(subject: str, subject_type: str = "dataset") -> str:
    """查询数据集/模型库的详情，主要是拿到 UUID 和是否有写权限。

    subject: 全名 owner/name，例如 scratch-repo。
    subject_type: dataset（数据集）或 model（模型库）。
    返回的 id 用作 openi_start_task 的 dataset_uuid_str / pretrain_model_id_str。
    """
    try:
        with _client() as client:
            info = client.subject_info(subject, subject_type)
        slim = {
            "id": info.get("id"),
            "name": info.get("name"),
            "owner_name": info.get("owner_name"),
            "alias": info.get("alias"),
            "is_private": info.get("is_private"),
            "size": info.get("size"),
            "can_edit_file": info.get("can_edit_file")
            if subject_type == "dataset"
            else (info.get("permission") or {}).get("can_edit_file"),
        }
        return dump({"ok": True, "subject_type": subject_type, "info": slim})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_upload_dataset_files(
    dataset: str,
    paths: list[str],
    remote_dir: str = "",
    dry_run: bool = False,
) -> str:
    """把本地文件/目录上传到数据集。小文件走直传、超过 64MB 自动分片续传。

    dataset: 数据集全名 owner/name。
    paths: 本地文件或目录路径列表；目录会递归上传并保留目录结构。建议用绝对路径，
    相对路径按 MCP 服务进程的工作目录解析。
    remote_dir: 仓内前缀目录，例如 "train/images"；留空则放在根目录。
    dry_run: True 时不联网，只回显会上传哪些文件、总量多大、各走哪条通道——传
      几十 GB 之前先跑一次。注意它只统计，不查剩余配额。

    返回里每个文件带 mode（direct/multipart）与是否成功。数据集里会自带一个
    openi_resource.version，属于正常现象。
    """
    try:
        entries = expand_upload_paths(paths, remote_dir=remote_dir)
        with _client() as client:
            report = client.upload_files(dataset, "dataset", entries, dry_run=dry_run)
        return dump({"ok": report.get("failed_count", 0) == 0, **report})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_upload_model_files(
    model: str,
    paths: list[str],
    remote_dir: str = "",
    dry_run: bool = False,
) -> str:
    """把本地文件/目录（例如 ckpt 权重）上传到模型库，规则同数据集上传。

    model: 模型全名 owner/name。
    paths: 本地文件或目录路径列表；目录会递归上传并保留目录结构。建议用绝对路径，
    相对路径按 MCP 服务进程的工作目录解析。
    remote_dir: 仓内前缀目录，例如 "v1"；留空则放在根目录。
    dry_run: True 时不联网，只回显清单与总量，先确认要传的就是这些。

    大文件（>64MB）自动按 64MB 分片上传，可断点续传（重复调用会跳过已完成分片）。
    """
    try:
        entries = expand_upload_paths(paths, remote_dir=remote_dir)
        with _client() as client:
            report = client.upload_files(model, "model", entries, dry_run=dry_run)
        return dump({"ok": report.get("failed_count", 0) == 0, **report})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def _resolve_subject_ids(client: OpenIClient, targets: list[str], subject_type: str) -> list[str]:
    ids: list[str] = []
    for target in targets:
        if "/" in target:
            ids.append(client.subject_id(target, subject_type))
        else:
            ids.append(target)
    return ids


def _resolve_mount_ids(client: OpenIClient, value: Optional[str], subject_type: str) -> Optional[str]:
    """把挂载参数里的全名 owner/name 换成 UUID，UUID 原样保留。

    平台创建任务的字段是逗号分隔的 UUID 串；这里允许直接写数据集/模型全名
    （来自 openi_search_datasets / openi_search_models），方便挂载平台托管的
    数据集与 ckpt，无需先手动查 UUID。
    """
    if not value:
        return value
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    return ",".join(_resolve_subject_ids(client, parts, subject_type))


@mcp.tool()
def openi_delete_dataset(datasets: list[str]) -> str:
    """删除数据集（不可恢复）。datasets 里可写全名 owner/name 或数据集 UUID。"""
    try:
        with _client() as client:
            ids = _resolve_subject_ids(client, datasets, "dataset")
            return dump({"ok": True, "deleted": client.delete_subjects("dataset", ids), "ids": ids})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_delete_model(models: list[str]) -> str:
    """删除模型库（不可恢复）。models 里可写全名 owner/name 或模型 UUID。"""
    try:
        with _client() as client:
            ids = _resolve_subject_ids(client, models, "model")
            return dump({"ok": True, "deleted": client.delete_subjects("model", ids), "ids": ids})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_storage_usage(subject: Optional[str] = None, subject_type: str = "dataset") -> str:
    """查看存储配额。不传 subject 时返回账号总体配额（上限/已用/剩余）。

    subject: 可选，数据集/模型全名 owner/name，看单个主体的占用。
    subject_type: subject 存在时有效，dataset 或 model。
    平台单账号默认 100GB 配额，上传前会先比对剩余额度。注意该接口即使带上
    subject 实测仍返回账号级汇总，所以单主体数字只能作参考。
    """
    try:
        with _client() as client:
            if subject:
                subject_id = client.subject_id(subject, subject_type)
                summary = client.storage_summary(subject_id, {"dataset": "1", "model": "2"}[subject_type])
                return dump({"ok": True, "scope": subject, "subject_type": subject_type,
                             "subject_id": subject_id, "storage": summary})
            return dump({"ok": True, "scope": "account", "storage": client.storage_summary()})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_search_datasets(
    q: str = "",
    scope: str = "public",
    order_by: str = "",
    tags: Optional[list[str]] = None,
    tasks: Optional[list[str]] = None,
    license: Optional[str] = None,
    recommend: bool = False,
    page: int = 1,
    page_size: int = 30,
) -> str:
    """检索平台托管的数据集，用来挑一个现成的数据集直接拉取或挂载。

    q: 关键词，按名称/别名模糊匹配，留空返回全部。
    scope: public（平台公开托管的全部数据集）/ owned（自己创建的）/
      collected（我收藏的）/ collaborated（协作的）。
    order_by: 留空按默认，可选 newest / recentupdate / downloadcount /
      collections / alias_asc / alias。
    tags: 类别过滤，例如 ["computer_vision"]；tasks: 任务过滤，例如 ["image_classification"]。
      两者也接受逗号分隔的字符串，例如 "computer_vision,natural_language_processing"。
    license: 许可证过滤，例如 "MIT"、"CC-BY-NC"、"Apache 2.0"（平台支持，实测能筛）。
    recommend: True 只看官方推荐（平台首页那批）。
    page / page_size: 分页，默认第 1 页、每页 30 条；page_size 上限 100（再大平台会
      静默退回 12 条，所以这里直接报错）。平台总量上万，要翻页得自己加 page。

    返回里每项含 subject（owner/name，可直接传给 openi_pull_dataset /
    openi_start_task）、id（UUID）、size_gb、downloads、stars、tags、tasks。
    """
    try:
        with _client() as client:
            result = client.search_subjects(
                "dataset", q=q, scope=scope, order_by=order_by, tags=tags,
                tasks=tasks, license=license, recommend=recommend, page=page, page_size=page_size,
            )
        return dump({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_search_models(
    q: str = "",
    scope: str = "public",
    order_by: str = "",
    tags: Optional[list[str]] = None,
    recommend: bool = False,
    page: int = 1,
    page_size: int = 30,
) -> str:
    """检索平台托管的模型/ckpt，用来挑一个现成的权重直接拉取或挂载。

    q: 关键词（例如 "llama"、"chatglm"、"stable-diffusion"），留空返回全部。
    scope: public（平台公开托管的全部模型）/ owned（自己的）/ collected（收藏）/
      collaborated。
    order_by: 留空按默认，可选 newest / recentupdate / downloadcount /
      collections / alias_asc / alias。
    tags: 标签过滤，接受列表或逗号分隔的字符串。
    recommend: True 只看官方推荐（llama2、chatglm3 等）。
    page / page_size: 分页，默认第 1 页、每页 30 条，上限 100。
    注意模型接口实测忽略 tasks / license 过滤（传了也不筛，等于全量），所以这里
    没有这两个参数；要按类别找模型请用 q 或 tags。

    返回里每项含 subject（owner/name，可直接传给 openi_pull_model /
    openi_start_task 的 pretrain_model_id_str）、id（模型 ID）、size_gb、
    downloads、stars、label、external（是否是外链模型，外链没有平台内的文件）。
    """
    try:
        with _client() as client:
            result = client.search_subjects(
                "model", q=q, scope=scope, order_by=order_by, tags=tags,
                recommend=recommend, page=page, page_size=page_size,
            )
        return dump({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_subject_files(
    subject: str,
    subject_type: str = "dataset",
    parent_dir: str = "",
    marker: str = "",
    page_size: int = 1000,
) -> str:
    """列出某个数据集/模型库里的文件与子目录（含每个文件的大小）。

    subject: 全名 owner/name（可用 openi_search_datasets / openi_search_models 查）。
    subject_type: dataset 或 model。
    parent_dir: 仓内子目录，留空为根目录。
    marker: 翻页游标，留空为第一页。单个目录最多返回 1000 行；返回里 has_next 为
      true 时，把返回的 marker 原样传回来取下一页（例如 20 万张图片的目录要翻 200 页）。
    page_size: 每次最多 1000 行，一般不用改。
    用来在 openi_pull_dataset / openi_pull_model 之前确认要拉哪些文件。
    """
    try:
        with _client() as client:
            page = client.subject_files(subject, subject_type, parent_dir, marker, page_size) or {}
        rows = [
            {
                "name": (f"{parent_dir}/{r.get('FileName')}".strip("/") if parent_dir else r.get("FileName")),
                "is_dir": bool(r.get("IsDir")),
                "size": r.get("Size"),
                "modified": r.get("ModTime"),
            }
            for r in (page.get("file_list") or [])
        ]
        return dump({"ok": True, "subject": subject, "dir": parent_dir, "has_next": page.get("has_next"),
                     "marker": page.get("marker") or "", "files": rows})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def _pull(client: OpenIClient, subject: str, subject_type: str, dest_dir: str, file: Optional[str],
          parent_dir: str, dry_run: bool, overwrite: bool, max_bytes: Optional[int],
          max_files: Optional[int] = None) -> str:
    report = client.pull_subject(
        subject, subject_type, dest_dir, file=file, parent_dir=parent_dir,
        dry_run=dry_run, overwrite=overwrite, max_bytes=max_bytes, max_files=max_files,
    )
    ok = report.get("failed_count", 0) == 0
    return dump({"ok": ok, **report})


@mcp.tool()
def openi_pull_dataset(
    dataset: str,
    dest_dir: str,
    file: Optional[str] = None,
    parent_dir: str = "",
    dry_run: bool = False,
    overwrite: bool = False,
    max_bytes: Optional[int] = None,
    max_files: Optional[int] = None,
) -> str:
    """把平台托管的数据集文件拉取到本地目录（从 OpenI 直接下载）。

    dataset: 数据集全名 owner/name，先用 openi_search_datasets 查，例如
      "Open_Dataset/imagenet"。
    dest_dir: 本地目标目录（会把仓内目录结构照搬下来）。
    file: 只拉这一个文件时给出仓内相对路径，例如 "imagenet/README.md"；留空则递归
      拉取整个数据集（或 parent_dir 指定的子目录）。
    parent_dir: file 为空时，只拉这个仓内子目录。
    dry_run: True 时只回显会下载哪些文件、多大，不真正下载——拉大文件前先跑一次。
    overwrite: 默认 False，本地已存在且大小一致的文件会跳过，方便续传。
    max_bytes: 总字节上限，超过直接报错（防止误拉几十 GB）。
    max_files: 遍历文件数上限，默认 20000（可用环境变量 OPENI_WALK_FILE_LIMIT 改）。
      文件更多的大数据集要显式调大才拉得全。

    数据集里自带的 openi_resource.version 默认跳过。
    """
    try:
        with _client() as client:
            return _pull(client, dataset, "dataset", dest_dir, file, parent_dir,
                         dry_run, overwrite, max_bytes, max_files)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_pull_model(
    model: str,
    dest_dir: str,
    file: Optional[str] = None,
    parent_dir: str = "",
    dry_run: bool = False,
    overwrite: bool = False,
    max_bytes: Optional[int] = None,
    max_files: Optional[int] = None,
) -> str:
    """把平台托管的模型/ckpt 文件拉取到本地目录（从 OpenI 直接下载）。

    model: 模型全名 owner/name，先用 openi_search_models 查，例如
      "FoundationModel/llama_model_7B"。
    dest_dir: 本地目标目录（会保留仓内目录结构）。
    file: 只拉某个文件时给出仓内相对路径；留空则递归拉取整个模型库。
    parent_dir: file 为空时只拉这个子目录。
    dry_run: True 时只回显文件名与大小，先看清体积再决定。
    overwrite: 默认 False，本地已存在且大小一致的文件跳过，便于续传。
    max_bytes: 总字节上限，超过直接报错。
    max_files: 遍历文件数上限，默认 20000，分片特别多的库要调大。

    ckpt 往往十几 GB，建议先 dry_run，或者直接用 openi_start_task 把它挂到云脑
    任务里（pretrain_model_id_str），不必下载到本地。
    """
    try:
        with _client() as client:
            return _pull(client, model, "model", dest_dir, file, parent_dir,
                         dry_run, overwrite, max_bytes, max_files)
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
    dataset_uuid_str / pretrain_model_id_str: 挂载的数据集 / 预训练模型，逗号分隔多个。
      既可以填 UUID，也可以直接填平台全名 owner/name（来自 openi_search_datasets /
      openi_search_models，或自己上传后拿到的库），工具会自动换成 UUID。挂载平台
      托管的公开数据集/ckpt 时用这个，不必下载到本地。
    has_internet: 0 不限制 / 1 无外网 / 2 有外网。
    time_limit: 自动停止时长（小时，-1 表示不自动停止）。
    visualize_required: 是否需要 TensorBoard。
    extra_fields: JSON 字符串，用于补充平台新增字段。
    """
    try:
        extra = json.loads(extra_fields) if extra_fields else None
        with _client() as client:
            dataset_uuid_str = _resolve_mount_ids(client, dataset_uuid_str, "dataset")
            pretrain_model_id_str = _resolve_mount_ids(client, pretrain_model_id_str, "model")
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


@mcp.tool()
def openi_points_today() -> str:
    """看今天（北京时间）已经拿到多少积分、还剩哪些没拿。

    返回今天的获取流水、当天的算力 10 分是否已到账，以及规则里"每日"类项目的
    完成情况。平台的每日结算以北京时间 0 点为界。
    """
    try:
        with _client() as client:
            status = autopoint.daily_status(client)
            config = client.points_rules()
        rules = config.get("TaskRules") or []
        reward = next((r for r in rules if r.get("TaskCode") == autopoint.REWARD_ACTION), {})
        earned_codes = set(status.get("today_actions") or [])
        pending_daily = []
        for rule in rules:
            if rule.get("RefreshRate") != "DAILY":
                continue
            code = rule.get("TaskCode")
            if code not in earned_codes:
                pending_daily.append(
                    {
                        "action": POINT_ACTIONS.get(code, code),
                        "task_code": code,
                        "points_per_cycle": float(rule.get("AwardAmount") or 0)
                        * float(rule.get("LimitNum") or 0),
                    }
                )
        pending_daily.sort(key=lambda row: -row["points_per_cycle"])
        return dump(
            {
                "ok": True,
                **status,
                "compute_reward_points": float(reward.get("AwardAmount") or 0),
                "pending_daily_actions": pending_daily,
                "automation": {
                    "automated_action": POINT_ACTIONS.get(autopoint.REWARD_ACTION),
                    "how": "openi_claim_daily_points 会在需要时开一个 1 分/卡时的 DEBUG 任务，"
                    "进入 RUNNING 后立即停止并删除；不足 6 分钟不计费，所以当天净赚 10 分、"
                    "算力消耗 0 分。其余每日项（commit/PR/issue/评论/建仓建数据集等）属于"
                    "仓库与社交行为，平台对重复公开项目、空评论等按刷分处理，不做自动化。",
                },
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_claim_daily_points(
    repo: Optional[str] = None,
    compute_source: Optional[str] = None,
    job_type: str = "DEBUG",
    timeout: Optional[float] = None,
    dry_run: bool = False,
    force: bool = False,
) -> str:
    """自动领取当天的 10 分算力积分（"每日运行计算任务"）。

    流程：确认今天还没拿到 → 挑一个 1 分/卡时的规格（默认 GCU ENFLAME-T20，实测能拉起）
    → 建任务 → 等它进入 RUNNING → 立刻停止并删除。计费按 6 分钟一档、不足 6 分钟不计费，
    所以这一步当天净赚 10 分、算力消耗 0 分，同时也覆盖了当天 10 卡时的用量。

    一天内多次开任务只有第一次给分（已核 185 条历史流水，没有任何一天拿到两次），
    所以每天跑一次即可，重复调用会直接跳过。

    repo: 代码仓 owner/name，留空自动选账号下第一个非空仓库。
    compute_source: 指定算力类型（GCU/MLU/ILUVATAR-GPGPU/BIREN-GPU），留空自动挑最便宜的。
    timeout: 等 RUNNING 的最长秒数，默认 900；排队不计费，可以等久一点。
    dry_run: 只报告将要创建什么，不真的建任务。
    force: 即使今天已经拿到也照常跑一遍，用来验证链路。

    注意：这个调用会阻塞到任务跑完（正常 1-5 分钟，最长 timeout），期间不要重复调用。
    """
    try:
        with _client() as client:
            report = autopoint.claim_daily_points(
                client,
                repo=repo,
                job_type=job_type,
                compute_source=compute_source,
                timeout=timeout,
                dry_run=dry_run,
                force=force,
            )
        return dump(report)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_run_on_card(
    code: str,
    compute_source: Optional[str] = None,
    spec_id: Optional[int] = None,
    repo: Optional[str] = None,
    job_type: str = "DEBUG",
    wait_seconds: float = 900.0,
    exec_timeout: float = 300.0,
    keep: bool = False,
) -> str:
    """在启智云脑的一张卡上直接跑一段 Python 代码，把 stdout 拿回来。

    平台没有 SSH，DEBUG 任务唯一的交互入口是它自带的 JupyterLab。这个工具会：
    挑代码仓和规格（默认最便宜的，通常是 1 分/卡时的 GCU ENFLAME-T20）→ 建 DEBUG
    任务 → 等 RUNNING → 取 debug_url → 连它的内核执行 code → 收 stdout → 停掉并删除任务。

    计费的只有 RUNNING 那段时间，不足 6 分钟不计费，所以跑个 print、查个
    npu-smi/nvidia-smi、测一下 torch.cuda.is_available() 这类短探测通常净消耗 0 分。

    code: 要执行的 Python 代码（可以用 "!cmd" 跑 shell）。
    compute_source: 算力类型，如 GCU/MLU/NPU/GPU/DCU/ILUVATAR-GPGPU/METAX-GPGPU/BIREN-GPU。
    spec_id: 指定规格 id（优先于 compute_source），先用 openi_list_creation_options 查。
    repo: 代码仓 owner/name，留空自动选账号下第一个非空仓库。
    wait_seconds: 等 RUNNING 的最长秒数，默认 900；排队不计费，可以等久一点。
    exec_timeout: 单段代码执行上限秒数，默认 300。
    keep: 跑完保留任务（不删），返回 Jupyter 地址供手动继续用；默认跑完即清理。
    返回报告里的 result.stdout 就是代码的输出。

    注意：这个调用会阻塞到跑完（正常几十秒到几分钟）；同时只能有 1 个 running
    DEBUG 任务，保留任务（keep=True）会挡住其他任务创建。
    """
    try:
        with _client() as client:
            report = exec_on_card.run_on_card(
                client,
                code,
                repo=repo,
                spec_id=spec_id,
                compute_source=compute_source,
                job_type=job_type,
                wait_seconds=wait_seconds,
                exec_timeout=exec_timeout,
                keep=keep,
            )
        return dump(report)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_autopoint_schedule() -> str:
    """给出把"每日自动领积分"挂到系统定时任务上的现成配置。

    返回 launchd（macOS）plist 与 crontab（Linux/macOS）两套写法，以及对应的
    命令路径（就是当前这个 Python 解释器）。把 plist 存到
    ~/Library/LaunchAgents/cn.openi.autopoint.plist 后 `launchctl load` 即可。
    """
    import sys

    python = sys.executable
    cwd = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    log = os.path.expanduser("~/Library/Logs/openi-autopoint.log")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>cn.openi.autopoint</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>openi_bridge.autopoint</string>
    <string>--json</string>
  </array>
  <key>WorkingDirectory</key><string>{cwd}</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>5</integer></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    cron = (
        f"5 9 * * * cd {cwd} && {python} -m openi_bridge.autopoint --json >> ~/.openi/autopoint.log 2>&1\n"
        f"*/30 * * * * cd {cwd} && {python} -m openi_bridge.autopoint --reap >> ~/.openi/autopoint.log 2>&1"
    )
    return dump(
        {
            "ok": True,
            "python": python,
            "working_directory": cwd,
            "plist_path": "~/Library/LaunchAgents/cn.openi.autopoint.plist",
            "launchd_plist": plist,
            "cron_lines": cron,
            "notes": [
                "令牌走 ~/.openi/token.json 或 OPENI_TOKEN，plist 里不用再塞令牌。",
                "第二条 cron 是兜底：万一某次跑崩了，半小时内也会把 autopoint-* 任务停掉删掉。",
                "手动跑一次验证：python -m openi_bridge.autopoint --status",
            ],
        }
    )


def main() -> None:
    """Entry point used by the openi-bridge console script."""
    # 诊断信息一律走 stderr：stdout 上跑的是 MCP 的 stdio 协议，混进一行非 JSON
    # 的文本会直接把客户端的解析弄崩。
    if not os.getenv("OPENI_TOKEN") and not os.path.exists(
        os.path.expanduser("~/.openi/token.json")
    ):
        print(
            "提示：未检测到令牌，工具调用会返回 NO_TOKEN 错误。请在 "
            "https://openi.pcl.ac.cn/user/settings/applications 生成令牌，"
            "然后 export OPENI_TOKEN=... 或写入 ~/.openi/token.json。",
            file=sys.stderr,
        )
    mcp.run("stdio")


if __name__ == "__main__":
    main()
