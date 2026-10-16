"""MCP server exposing OpenI (启智社区) compute as tools.

Run with:  OPENI_TOKEN=... python -m openi_bridge.server
or through the console script:  openi-bridge
"""

from __future__ import annotations

import hashlib
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
    MAX_REPO_FILE_BYTES,
    OpenIClient,
    OpenIError,
    dump,
    expand_upload_paths,
    summarize_spec,
    summarize_task_template,
    template_image_url,
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
        "（数据集、ckpt 权重等）上传上去，再把得到的 UUID 挂到计算任务上。"
        "代码仓本身也能读写：openi_create_repository 建仓，openi_push_repo_files / "
        "openi_write_repo_file 把代码提交上去，不需要本地 git；openi_repo_tree 一次"
        "拿全文件树，openi_list_repo_commits 看提交历史，openi_download_repo_archive "
        "打包下载整仓，openi_create_repo_issue / openi_comment_repo_issue 在仓里提 issue "
        "与评论，openi_update_repo_issue 关闭或改动 issue。仓的只读治理面另有 "
        "openi_list_repo_tags / openi_list_repo_releases / openi_list_repo_labels / "
        "openi_list_repo_collaborators / openi_list_repo_stargazers；想找别人的仓用 "
        "openi_list_starred_repositories / openi_list_subscribed_repositories / "
        "openi_search_topics / openi_search_users，自己加入的组织用 openi_list_my_orgs。"
        "通知用 openi_notifications "
        "列表、openi_notification_detail 看正文、openi_mark_all_notifications_read 全部标已读。"
        "数据集/模型库里的文件也能单个删除：openi_delete_subject_file。创建任务前先用 "
        "openi_list_creation_options 拿到实时的 spec_id 与镜像；openi_list_task_templates "
        "列出别人配好的一键运行模板，openi_start_task_from_template 可以直接照着模板开任务"
        "（模板自带代码仓/分支/启动文件/数据集挂载），openi_list_compute_resources 看平台"
        "在线的算力中心与卡型。在线推理任务（job_type=ONLINEINFERENCE）用 openi_start_task "
        "建，容器里的服务监听 8000 端口，RUNNING 之后用 openi_task_service_url 取对外地址。"
        "使用前必须设置 "
        "OPENI_TOKEN 环境变量，令牌在 https://openi.pcl.ac.cn/user/settings/applications 生成。"
        "任务跑完后用 openi_task_outputs / openi_task_output_files 看产出文件、"
        "openi_task_download_output 取单个文件、openi_task_download_output_archive 整包"
        "打 zip 下载、openi_task_download_log 取整份日志；"
        "任务排队或失败时用 openi_task_events 看调度时间线，openi_task_nodes 看节点，"
        "openi_task_loss 看训练 loss 曲线。训练产物要长期留存就用 "
        "openi_task_export_to_dataset / openi_task_export_to_model 把它导成数据集或模型库，"
        "openi_task_export_progress 查导出进度。"
    ),
)


def _client() -> OpenIClient:
    return OpenIClient()


def _err(exc: Exception) -> str:
    """Render an exception as the tool's error envelope.

    ``status_code`` is carried through on purpose: callers branch on 401/403
    versus "no token" (see the 403 handling in the repo-file tools), and
    without it the only way to tell them apart is string matching on the
    message, which breaks the moment the wording changes.
    """
    payload: dict[str, Any] = {"ok": False, "error": str(exc)}
    status = getattr(exc, "status_code", None)
    if status is not None:
        payload["status_code"] = status
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _ok(value: Any, **extra: Any) -> str:
    if isinstance(value, dict):
        merged = dict(value)
        merged.update(extra)
        return dump(merged)
    return dump({"result": value, **extra})


def _bounded_ok(report: Any) -> dict[str, Any]:
    """去掉 report 自带的 ok，让调用方显式决定这个字段。

    批量操作返回的 report 里可能也带一个 ok；用 ``{"ok": X, **report}`` 拼结果时
    它会被 report 的值盖掉，于是判定结果和实际内容不一致。约定是：先摊开 report，
    再显式写 ok。
    """
    if not isinstance(report, dict):
        return {"report": report}
    return {k: v for k, v in report.items() if k != "ok"}


def _slim_output_files(files: Any) -> list[dict[str, Any]]:
    """把 /ai_task/output* 的 file_list 压成 name / is_dir / size / modified。"""
    slim: list[dict[str, Any]] = []
    for entry in files or []:
        if not isinstance(entry, dict):
            continue
        slim.append(
            {
                "name": entry.get("FileName"),
                "is_dir": bool(entry.get("IsDir")),
                "size": entry.get("Size"),
                "modified": entry.get("ModTime") or None,
            }
        )
    return slim


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
def openi_list_compute_resources() -> str:
    """查平台实时上线的算力中心与卡型（比离线目录更全）。

    回答的是“平台现在有什么”：哪些算力中心在线、在哪个城市、各有哪些卡型
    （含 CPU 的 INTEL XEON、GPGPU 的 N100/N260、GPU 的 5090/A800 这些离线目录
    里没有的）。它不代表账号现在能开哪些规格——那要用 openi_list_creation_options
    拿实时 spec_id、单价与配额。价格参考 openi_list_resources。
    """
    try:
        with _client() as client:
            data = client.compute_resources()
        cards = [
            {"compute_source": row.get("ComputeSource"), "card_types": row.get("CardList") or []}
            for row in data.get("acc_card_types") or []
            if isinstance(row, dict)
        ]
        centers = [
            {
                "code": row.get("AICenterCode"),
                "name": row.get("AICenterName"),
                "city": row.get("City"),
                "province": row.get("Province"),
                "compute_sources": row.get("ComputeSourceList") or [],
                "card_types": row.get("AccCardTypeList") or [],
            }
            for row in data.get("ai_centers") or []
            if isinstance(row, dict)
        ]
        return dump(
            {
                "ok": True,
                "acc_card_types": cards,
                "ai_centers": centers,
                "note": "这是平台在线资源目录；能开哪些规格（spec_id、单价、配额）见 openi_list_creation_options。",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_notifications(limit: int = 20, page: int = 1, unread_only: bool = False) -> str:
    """查看账号通知（有人评论、@ 或给你的仓库提 issue 时会收到）。

    unread_only: True 时只列未读；返回里带 unread 未读总数。
    """
    try:
        with _client() as client:
            items = client.notifications(limit=limit, page=page, unread=unread_only)
            count = client.unread_notification_count()
        slim = [
            {
                "id": n.get("id"),
                "unread": n.get("unread"),
                "type": (n.get("subject") or {}).get("type")
                if isinstance(n.get("subject"), dict)
                else None,
                "title": (n.get("subject") or {}).get("title")
                if isinstance(n.get("subject"), dict)
                else None,
                "url": ((n.get("subject") or {}).get("url") or "").replace("/api/v1/repos/", "/")
                if isinstance(n.get("subject"), dict)
                else None,
                "repo": (n.get("repository") or {}).get("full_name")
                if isinstance(n.get("repository"), dict)
                else None,
                "updated_at": n.get("updated_at"),
            }
            for n in (items or [])
            if isinstance(n, dict)
        ]
        unread = count.get("new") if isinstance(count, dict) else count
        return dump({"ok": True, "unread": unread, "count": len(slim), "notifications": slim})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_notification_detail(thread_id: str, with_comments: bool = True) -> str:
    """看单条通知的完整内容（列表接口只给标题和链接）。

    thread_id: openi_notifications 返回里每项的 id。
    with_comments: True（默认）时连这条通知下的评论正文一起返回，省一次调用。
    """
    try:
        with _client() as client:
            thread = client.notification_thread(thread_id)
            comments = (
                client.notification_thread_comments(thread_id) if with_comments else []
            )
        return dump({"ok": True, **thread, "comments": comments})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_mark_all_notifications_read() -> str:
    """把所有未读通知一次标为已读。

    平台只提供“全部已读”这一条接口；Gitea 那套“标记单条通知”走的是被前置
    反向代理挡掉的 PATCH，令牌方式用不了，所以这里只做全清。
    """
    try:
        with _client() as client:
            client.mark_notifications_read()
        return dump({"ok": True, "marked_all_read": True})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


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
def openi_list_repo_tags(repo: str) -> str:
    """列出代码仓的 tag 及每个 tag 指向的提交。

    用来确认某个版本打没打过 tag、tag 落在哪个提交上。注意从 GitHub 同步过来的
    mirror 仓，它的 ref 在平台侧是只读的：tag 删不掉、也推不动，返回里会正常列出。
    """
    try:
        with _client() as client:
            tags = client.repo_tags(repo)
        return dump({"ok": True, "repo": repo, "count": len(tags), "tags": tags})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_repo_releases(repo: str) -> str:
    """列出代码仓的 release（版本发布），含 tag、草稿/预发布标记与时间。"""
    try:
        with _client() as client:
            releases = client.repo_releases(repo)
        return dump({"ok": True, "repo": repo, "count": len(releases), "releases": releases})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_repo_labels(repo: str) -> str:
    """列出代码仓的标签（issue 分类用），含颜色。"""
    try:
        with _client() as client:
            labels = client.repo_labels(repo)
        return dump({"ok": True, "repo": repo, "count": len(labels), "labels": labels})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_repo_collaborators(repo: str) -> str:
    """列出代码仓的协作者（login / full_name / is_active）。

    注意平台这个接口不回 permissions 字段（2026-10-17 实测），所以这里不承诺给出
    每个人的权限；判断自己对该仓有没有写权限要靠别的手段（例如试着读仓的 right
    接口，目前没有对应的 MCP 工具）。
    """
    try:
        with _client() as client:
            people = client.repo_collaborators(repo)
        return dump({"ok": True, "repo": repo, "count": len(people), "collaborators": people})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_repo_stargazers(repo: str, limit: int = 50, page: int = 1) -> str:
    """列出收藏（star）某个代码仓的账号。"""
    try:
        with _client() as client:
            people = client.repo_stargazers(repo, limit=limit, page=page)
        return dump({"ok": True, "repo": repo, "count": len(people), "stargazers": people})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_starred_repositories(limit: int = 50, page: int = 1) -> str:
    """列出当前账号收藏（star）的代码仓。

    和 openi_list_my_repositories 不同：那个只列自己 owner 的仓，这里列的是别人
    的仓（含组织下的），是找参考实现和可复用代码的入口。
    """
    try:
        with _client() as client:
            repos = client.account_repos("starred", limit=limit, page=page)
        return dump({"ok": True, "count": len(repos), "repositories": repos})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_subscribed_repositories(limit: int = 50, page: int = 1) -> str:
    """列出当前账号关注（watch）的代码仓。"""
    try:
        with _client() as client:
            repos = client.account_repos("subscriptions", limit=limit, page=page)
        return dump({"ok": True, "count": len(repos), "repositories": repos})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_my_orgs() -> str:
    """列出当前账号加入的组织 / 团队。"""
    try:
        with _client() as client:
            orgs = client.my_orgs()
        return dump({"ok": True, "count": len(orgs), "orgs": orgs})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_search_topics(query: str = "", limit: int = 20) -> str:
    """按主题（topic）找仓，主题上带着仓数量。"""
    try:
        with _client() as client:
            topics = client.search_topics(query, limit=limit)
        return dump({"ok": True, "count": len(topics), "topics": topics})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_search_users(query: str, limit: int = 20) -> str:
    """按关键字找账号（人和组织），拿 login 用于拼全名 owner/name。"""
    try:
        with _client() as client:
            users = client.search_users(query, limit=limit)
        return dump({"ok": True, "count": len(users), "users": users})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_create_repository(
    name: str,
    description: str = "",
    private: bool = False,
    auto_init: bool = True,
    default_branch: str = "master",
) -> str:
    """新建一个代码仓，仓全名固定为 当前账号登录名/name。

    建完可以用 openi_push_repo_files 把本地代码传上去，再用
    openi_start_task 跑任务——不需要本地 git。

    name: 仓名，只接受字母/数字/连字符/下划线/点。
    description: 仓描述，可留空。
    private: True 建私有仓。
    auto_init: True 初始化一个 README，仓里就有一个默认分支可写；False 建空仓。
    default_branch: 初始化时的默认分支名，默认 master。

    注意：平台建仓很慢（实测 30 秒以上），超时不代表失败——请用
    openi_list_my_repositories 回查确认，不要重复建仓。
    """
    try:
        with _client() as client:
            repo = client.create_repo(
                name,
                description=description,
                private=private,
                auto_init=auto_init,
                default_branch=default_branch,
            )
        info = repo if isinstance(repo, dict) else {}
        # 建仓响应里的 default_branch 常是空的（初始化还没跑完），补查一次。
        branch = info.get("default_branch")
        if not branch and info.get("full_name"):
            with _client() as client:
                branch = client.default_branch(info["full_name"])
        return dump(
            {
                "ok": True,
                "full_name": info.get("full_name"),
                "default_branch": branch,
                "private": info.get("private"),
                "html_url": info.get("html_url"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_delete_repository(repo: str) -> str:
    """删掉一个代码仓，连同里面的代码和提交历史，不可恢复。

    只删仓库本身；仓里跑过的任务和数据集/模型库不受影响，需要的话分别用
    openi_delete_task / openi_delete_dataset / openi_delete_model 处理。

    repo: 代码仓全名 owner/name。平台删仓同样偏慢，会在服务端先跑一会儿。
    """
    try:
        with _client() as client:
            client.delete_repo(repo)
        return dump({"ok": True, "repo": repo, "deleted": True})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_repo_files(repo: str, ref: Optional[str] = None, path: str = "") -> str:
    """列出代码仓某个目录与全部分支，用于确定启动文件 boot_file（例如 train.py）。

    repo: 代码仓全名 owner/name。
    ref: 分支或提交，留空用默认分支。
    path: 仓内目录，留空是根目录，例如 "src" 或 "src/models"。
    """
    try:
        with _client() as client:
            files = client.repo_contents(repo, ref=ref, path=path)
            branches = client.repo_branches(repo)
        if isinstance(files, list):
            entries = [
                {"name": f.get("name"), "type": f.get("type"), "size": f.get("size")}
                for f in files
                if isinstance(f, dict)
            ]
        else:
            entries = files
        branch_names = [b.get("name") for b in branches] if isinstance(branches, list) else branches
        return dump({"ok": True, "path": path, "files": entries, "branches": branch_names})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_repo_tree(
    repo: str, ref: Optional[str] = None, path: str = "", max_entries: int = 20000
) -> str:
    """一次拿到代码仓的完整文件树，用来确认目录结构和启动入口在哪。

    比逐层调 openi_list_repo_files 快得多：平台一条请求就返回整棵树，适合先看清
    仓里有什么再决定读哪个文件。

    repo: 代码仓全名 owner/name。
    ref: 分支或提交，留空用 HEAD（默认分支的最新提交）。
    path: 只看这个子目录；客户端过滤，请求本身仍取全量。
    max_entries: 最多返回多少条，默认 20000；被截断时 truncated 为 true。
    """
    try:
        with _client() as client:
            result = client.repo_tree(repo, ref=ref or "", path=path, max_entries=max_entries)
        return dump({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_repo_commits(
    repo: str, ref: Optional[str] = None, limit: int = 20, page: int = 1
) -> str:
    """列出代码仓的提交历史（按时间倒序），用来核对代码是不是真的推上去了。

    repo: 代码仓全名 owner/name。
    ref: 分支或提交，留空用默认分支。
    limit / page: 分页，默认最新 20 条。

    平台的 path 过滤参数在这套部署上是空操作（实测传任何 path 都返回全量历史），
    所以这里没有这个参数；要看某个具体文件的内容直接 openi_read_repo_file。
    """
    try:
        with _client() as client:
            commits = client.repo_commits(repo, ref=ref or "", limit=limit, page=page)
        return dump({"ok": True, "repo": repo, "count": len(commits), "commits": commits})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_download_repo_archive(
    repo: str, save_to: str, ref: Optional[str] = None, max_bytes: Optional[int] = None
) -> str:
    """把整个代码仓打包成 zip 下载到本地（备份，或整份拿到本地再改）。

    只想看单个文件用 openi_read_repo_file，只要目录结构用 openi_repo_tree。

    repo: 代码仓全名 owner/name。
    save_to: 本地保存路径；如果给的是已存在的目录，会按 <仓名>-<ref>.zip 命名。
    ref: 分支或提交，留空用默认分支。
    max_bytes: 大小上限，超了立刻中止并删掉半包，不会留下残缺的 zip。
    """
    try:
        with _client() as client:
            result = client.download_repo_archive(
                repo, save_to, ref=ref or "", max_bytes=max_bytes
            )
        return dump({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_create_repo_issue(repo: str, title: str, body: str = "") -> str:
    """在代码仓里开一个 issue，用来报问题或记待办，返回 issue 号。

    repo: 代码仓全名 owner/name。
    title: 标题（必填）。
    body: 正文，支持 Markdown，可留空。
    返回里的 number 可以接着传给 openi_comment_repo_issue。
    """
    try:
        with _client() as client:
            issue = client.create_repo_issue(repo, title, body)
        return dump({"ok": True, "repo": repo, "issue": issue})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_repo_issues(
    repo: str, state: str = "open", limit: int = 20, page: int = 1
) -> str:
    """列出代码仓的 issue，拿到 issue 号后可以用 openi_comment_repo_issue 回复。

    repo: 代码仓全名 owner/name。
    state: open（默认）/ closed / all。
    limit / page: 分页，默认第 1 页、每页 20 条。
    """
    try:
        with _client() as client:
            issues = client.repo_issues(repo, state=state, limit=limit, page=page)
        return dump({"ok": True, "repo": repo, "state": state, "count": len(issues), "issues": issues})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_comment_repo_issue(repo: str, index: int, body: str) -> str:
    """在某个 issue 下发表评论。

    repo: 代码仓全名 owner/name。
    index: issue 号，即 openi_list_repo_issues 返回的 number（也可以从
      openi_create_repo_issue 的返回值里拿）。
    body: 评论正文，支持 Markdown。
    """
    try:
        with _client() as client:
            comment = client.comment_repo_issue(repo, index, body)
        return dump({"ok": True, "repo": repo, "issue": index, "comment": comment})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_update_repo_issue(
    repo: str,
    index: int,
    state: Optional[str] = None,
    title: Optional[str] = None,
    body: Optional[str] = None,
) -> str:
    """改一个已有 issue：关闭 / 重开、改标题、改正文。

    repo: 代码仓全名 owner/name。
    index: issue 号，即 openi_list_repo_issues 返回的 number。
    state: open（重开）或 closed（关闭），不改就留空。
    title / body: 新的标题或正文，支持 Markdown，不改就留空。

    平台不接受空改动，state / title / body 至少要给一个。
    """
    try:
        with _client() as client:
            issue = client.update_repo_issue(
                repo, index, state=state, title=title, body=body
            )
        return dump({"ok": True, "repo": repo, "issue": issue})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_read_repo_file(
    repo: str,
    path: str,
    ref: Optional[str] = None,
    encoding: str = "utf-8",
    save_to: Optional[str] = None,
    max_bytes: int = 2_000_000,
) -> str:
    """读取代码仓里的一个文件，用于确认 boot_file 里的启动入口、依赖或超参。

    repo: 代码仓全名 owner/name。path: 仓内路径，例如 src/train.py。
    ref: 分支或提交，留空用默认分支。
    encoding: 按该编码解码成文本，默认 utf-8；解码不了说明是二进制文件。
    save_to: 给了就把原始字节写到本地这个路径（二进制或大文件用这条），返回 md5。
    max_bytes: 直接拒绝的大小上限，默认 2MB，0 表示不限制。
    """
    try:
        with _client() as client:
            data = client.read_repo_file(repo, path, ref=ref, max_bytes=max_bytes)
        blob = data.pop("bytes")
        if save_to:
            dest = os.path.abspath(os.path.expanduser(save_to))
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(blob)
            return dump(
                {
                    "ok": True,
                    **data,
                    "saved_to": dest,
                    "md5": hashlib.md5(blob).hexdigest(),
                }
            )
        try:
            text = blob.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            return dump(
                {
                    "ok": True,
                    **data,
                    "binary": True,
                    "note": f"按 {encoding} 解不出来，是二进制文件；需要落盘请传 save_to。",
                }
            )
        return dump({"ok": True, **data, "text": text})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_write_repo_file(
    repo: str,
    path: str,
    content: str,
    message: Optional[str] = None,
    branch: Optional[str] = None,
    dry_run: bool = False,
) -> str:
    """往代码仓写一个文本文件（新建或覆盖），走平台 contents 接口，不需要本地 git。

    典型用法：先 openi_push_repo_files 传代码，再用这个工具改 train.py 的
    超参或 boot_file 里的入口，然后 openi_start_task。

    repo: 代码仓全名 owner/name。path: 仓内路径。content: 文件正文（文本）。
    message: 提交信息，留空自动生成。branch: 目标分支，留空用默认分支。
    dry_run: True 时只回显会新建还是覆盖，不提交。
    """
    try:
        with _client() as client:
            if dry_run:
                exists = client.repo_file_sha(repo, path) is not None
                return dump(
                    {
                        "ok": True,
                        "dry_run": True,
                        "repo": repo,
                        "path": path,
                        "branch": branch,
                        "action": "update" if exists else "create",
                        "bytes": len(content.encode("utf-8")),
                    }
                )
            result = client.write_repo_file(
                repo, path, content, message=message or "", branch=branch
            )
        info = result.get("content") if isinstance(result, dict) else {}
        commit = result.get("commit") if isinstance(result, dict) else {}
        return dump(
            {
                "ok": True,
                "repo": repo,
                "path": (info or {}).get("path", path),
                "sha": (info or {}).get("sha"),
                "commit_sha": (commit or {}).get("sha"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_push_repo_files(
    repo: str,
    paths: list[str],
    remote_dir: str = "",
    message: Optional[str] = None,
    branch: Optional[str] = None,
    dry_run: bool = False,
    max_bytes: Optional[int] = None,
    max_file_bytes: int = MAX_REPO_FILE_BYTES,
) -> str:
    """把本地文件/目录提交进代码仓，每个文件一个提交——不用装 git 就能发文代码去跑任务。

    repo: 代码仓全名 owner/name。
    paths: 本地文件或目录路径列表；目录会递归收集并保留目录结构。建议绝对路径。
    remote_dir: 仓内前缀目录，例如 "src"；留空则放在根目录。
    message: 提交信息，留空自动生成。
    branch: 目标分支，留空用默认分支。
    dry_run: True 时只回显会上传哪些文件、共多少字节，不提交。
    max_bytes: 整批总量上限，超出直接拒绝。
    max_file_bytes: 单文件上限，默认 50MB——contents 接口用 base64 传、文件会进内存，
    更大的文件请改用 git 推送。
    """
    try:
        entries = expand_upload_paths(paths, remote_dir=remote_dir)
        with _client() as client:
            result = client.push_repo_files(
                repo,
                entries,
                message=message or "",
                branch=branch,
                dry_run=dry_run,
                max_bytes=max_bytes,
                max_file_bytes=max_file_bytes,
            )
        return dump({"ok": True, **result})
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
    engine: 引擎代号，默认 0（不指定）。平台没有公布取值表，所以这里只校验它是
      个非负整数；传错值平台会拒绝建库，不会静默建出一个错引擎的库。
    label / license: 展示用标签与许可证，可留空。
    """
    try:
        # name / alias 的规则与数据集一致，交给 client 校验；engine 平台没给取值表，
        # 只拦住明显非法的值（负数、非整数），而不是编一套平台不认的枚举。
        if isinstance(engine, bool) or not isinstance(engine, int):
            return dump({"ok": False, "error": f"engine 必须是整数：{engine!r}"})
        if engine < 0:
            return dump({"ok": False, "error": f"engine 不能为负数：{engine}"})
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
        # ok 写在后面：**report 里也可能有 ok，让它盖掉判定结果就等于自欺。
        return dump({**_bounded_ok(report), "ok": report.get("failed_count", 0) == 0})
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
        return dump({**_bounded_ok(report), "ok": report.get("failed_count", 0) == 0})
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


@mcp.tool()
def openi_delete_subject_file(
    subject: str, path: str, subject_type: str = "dataset"
) -> str:
    """删除数据集/模型库里的一个文件，或者连同内容删掉整个子目录（不可恢复）。

    subject: 全名 owner/name（可用 openi_search_datasets / openi_search_models 查）。
    path: 仓内相对路径，例如 "a.txt" 或 "ckpt/epoch1.pt"；写成目录名就删掉整个
      目录连同里面的文件，例如 "ckpt"。
    subject_type: dataset（默认）或 model。

    删除只影响这一个路径，库本身的其余内容与 UUID 不变。注意这个接口对不存在的
    路径也回 200/ok，所以工具删完会重新列一遍父目录核对：清单里确实没有了才报
    removed=true，本来就没有则报 existed=false（什么都没动）。
    """
    try:
        with _client() as client:
            result = client.delete_subject_file(subject, subject_type, path)
        return dump({"ok": result.get("removed", False), "subject": subject,
                     "subject_type": subject_type, **result})
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
            job_type = detail.get("job_type") or (
                (detail.get("task") or {}).get("job_type") if isinstance(detail.get("task"), dict) else ""
            )
            if str(job_type).upper() == "ONLINEINFERENCE":
                try:
                    detail["service"] = client.service_url(task_id)
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
def openi_task_logs(
    task_id: str,
    repo: Optional[str] = None,
    lines: int = 200,
    order: str = "desc",
    base_line: int = -1,
    node_id: Optional[str] = None,
    file_name: Optional[str] = None,
) -> str:
    """读取计算任务的运行日志，默认最近 200 行。

    训练任务写日志文件时，重复调用可以把最后一次的 base_line 传进来续读，
    用来跟进训练进度。

    order: desc 从尾部往前取（默认，看最新），asc 从头开始。
    base_line: 从这一行之后继续读；-1 表示从 order 指定的那一端开始。
    node_id: 多机任务里指定节点。
    file_name: 训练任务里指定日志文件名。
    """
    try:
        with _client() as client:
            logs = client.task_log(
                task_id,
                lines=lines,
                order=order,
                base_line=base_line,
                node_id=node_id,
                file_name=file_name,
                repo=repo,
            )
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
def openi_task_service_url(task_id: str) -> str:
    """获取在线推理（ONLINEINFERENCE）任务的对外服务地址。

    返回里的 url 就是可以直接访问的地址（形如 https://<hex>.mlunotebook.openi.org.cn），
    同时带上任务详情里的 port 与平台自动生成的 end_point 作为补充。

    只有 ONLINEINFERENCE 任务在 RUNNING 之后才有地址。服务还在预热时平台返回
    code 99（"The service is loading"），等一会儿再调用即可；容器里的服务必须
    监听 8000 端口，否则这个地址连不上。
    """
    try:
        with _client() as client:
            return dump({"ok": True, "service": client.service_url(task_id)})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_events(task_id: str) -> str:
    """看一个任务的调度事件时间线，用来定位“为什么排队 / 为什么失败”。

    平台按“分配节点 → 拉镜像 → 建容器 → 启动 → 结束”逐步记录，每条含
    message（人读的说明）、reason（阶段名，如 Scheduled / Pulled / Created /
    Started / Killing / Exit）、timestamp（UTC）与 action。任务长期卡在 WAITING、
    被抢占、镜像拉不动或异常退出时，这里能看出卡在哪一步、为什么。
    """
    try:
        with _client() as client:
            data = client.task_operation_profile(task_id)
        events = data.get("events") if isinstance(data, dict) else data
        return dump({"ok": True, "task_id": task_id, "events": events})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_nodes(task_id: str) -> str:
    """列出任务实际跑在哪些节点上（多机任务会有多条）。

    返回每个节点 {id, log_file_name}；配合 openi_task_logs 的 node_id 可以分别
    读取各节点的日志。
    """
    try:
        with _client() as client:
            return dump({"ok": True, "task_id": task_id, "nodes": client.task_node_info(task_id)})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_outputs(task_id: str, parent_dir: str = "") -> str:
    """列出任务输出目录里的文件（一次一层，子目录用 parent_dir 继续往下翻）。

    parent_dir: 相对输出根的子目录，留空看根目录。想一次拿全量递归清单就用
    openi_task_output_files。

    重要：平台对输出是异步索引的。任务刚跑完时 ready=false、file_list 为空
    （平台字段 status=1），通常要等一两分钟才会变成 ready=true 并列出文件
    （status=0）。刚结束查不到文件时，隔一会儿重试，别当成任务没有输出。
    """
    try:
        with _client() as client:
            data = client.task_output(task_id, parent_dir=parent_dir)
        out = data.get("output") if isinstance(data, dict) else {}
        out = out if isinstance(out, dict) else {}
        status = out.get("status")
        return dump(
            {
                "ok": True,
                "task_id": task_id,
                "parent_dir": parent_dir,
                "ready": status == 0,
                "status": status,
                "is_task_terminal": out.get("is_task_terminal"),
                "can_download": out.get("can_download"),
                "can_reschedule": out.get("can_reschedule"),
                "output_size_limit": out.get("output_size_limit"),
                "files": _slim_output_files(out.get("file_list")),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_output_files(task_id: str) -> str:
    """一次列出任务输出的全部文件（递归，含子目录里的）。

    返回每项的 name 就是相对输出根的完整路径，可以直接作为
    openi_task_download_output 的 file_name。刚跑完时同样要等平台索引完成
    （见 openi_task_outputs 的说明），列表为空不代表没有输出。
    """
    try:
        with _client() as client:
            data = client.task_output_all(task_id)
        out = data.get("output") if isinstance(data, dict) else {}
        out = out if isinstance(out, dict) else {}
        return dump(
            {
                "ok": True,
                "task_id": task_id,
                "files": _slim_output_files(out.get("file_list")),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_download_output(
    task_id: str,
    file_name: str,
    parent_dir: str = "",
    save_to: Optional[str] = None,
) -> str:
    """把任务输出的某个文件下载到本地（或直接读进返回里）。

    file_name: 文件名或相对输出根的完整路径。用 openi_task_output_files 拿到的
      name 直接填这里即可；根目录下的文件也可以只给文件名。
    parent_dir: file_name 只给纯文件名时，指定它所在子目录；用完整路径时留空。
    save_to: 本地保存路径。给目录（或以 / 结尾）时按原文件名存进去；不给则把
      内容读进返回（能按文本解码就直接给，二进制给 base64）。

    小文件（几 MB 以内）不带 save_to 直接读进来最省事；大文件务必给 save_to
    让它流式落盘。
    """
    try:
        with _client() as client:
            payload = client.task_output_download(
                task_id, file_name, parent_dir=parent_dir, save_to=save_to
            )
        return dump({"ok": True, "task_id": task_id, "file_name": file_name, **payload})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_download_output_archive(task_id: str, save_to: str, max_bytes: Optional[int] = None) -> str:
    """把任务的整个输出目录打成一个 zip 下到本地（产物多时比逐个文件拉省事）。

    task_id: 任务号。
    save_to: 本地保存路径（给目录或以 / 结尾就按 task-<id>-output.zip 存）。
      这条接口是流式落盘的，必须给落盘路径；文件很大时不会把内容读进内存。
    max_bytes: 大小硬上限，超了立即中止并删掉半截包（默认不限）。

    任务没有产物时平台会回一个空的 zip（约 22 字节），那是正常结果不是错误；
    下载前后可以用 openi_task_output_files 核对清单。
    """
    try:
        with _client() as client:
            payload = client.task_output_download_all(
                task_id, save_to=save_to, max_bytes=max_bytes
            )
        return dump({"ok": True, "task_id": task_id, **payload})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_download_log(
    task_id: str,
    save_to: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> str:
    """下载任务的完整日志（整段纯文本，不分页）。

    和 openi_task_logs 的区别：那个是分页读、适合跟进进度；这个是平台一次性给出
    的完整日志文本，适合整段留存或喂给别的工具分析。
    save_to: 本地保存路径（给目录就按 task-<id>.log 存）；不给则把文本读进返回
      （默认最多 256KB，超过截断并带 truncated 标记，防止日志把返回撑爆）。
    max_bytes: 自定义上限。给 save_to 时它是硬上限（超了立即中止，不落盘）；
      不给 save_to 时它是内存里的截断上限。
    """
    try:
        limit = max_bytes if (max_bytes is not None or save_to) else 256 * 1024
        with _client() as client:
            payload = client.task_log_download(task_id, save_to=save_to, max_bytes=limit)
        return dump({"ok": True, "task_id": task_id, **payload})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_loss(task_id: str) -> str:
    """读取训练任务的 loss 曲线（epoch / loss 序列）。

    任务没上报 loss 时返回空列表；这是平台侧记录的曲线，训练脚本没按平台方式
    输出 loss 时会为空。
    """
    try:
        with _client() as client:
            data = client.task_loss(task_id)
        return dump({"ok": True, "task_id": task_id, "loss": data})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_export_to_dataset(task_id: str, dataset: str, files: list[str]) -> str:
    """把任务输出文件搬进一个已有的数据集（训练产物落库的第一条路）。

    训练跑完的输出（ckpt、metrics、生成语料）默认只活在该任务的输出目录里，
    跟着任务走、任务清了就没了。导出之后它们变成数据集里的普通文件，可以挂到
    别的任务上、被别人拉取，也不受任务清理影响。

    task_id: 产出这些文件的任务号。
    dataset: 目标数据集全名 owner/name（数据集要已存在，用 openi_create_dataset
      新建后上传，或直接用别人的公开数据集）。
    files: 要导出的文件路径列表，相对输出根，即 openi_task_output_files 里的
      name；子目录里的文件要连父目录一起写，例如 ["metrics.txt","ckpt/model.bin"]。

    导出是异步的：返回里的 process_id 喂给 openi_task_export_progress 查进度。
    """
    try:
        with _client() as client:
            info = client.subject_info(dataset, "dataset")
            dataset_id = info.get("id") if isinstance(info, dict) else None
            if not dataset_id:
                raise OpenIError(f"没拿到数据集 {dataset} 的 UUID，确认名字 owner/name 是否正确。")
            result = client.export_task_to_dataset(task_id, dataset_id, files)
        return dump({"ok": True, "dataset": dataset, **result})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_export_to_model(
    task_id: str,
    name: str,
    files: list[str],
    alias: Optional[str] = None,
    license: str = "",
    is_private: bool = False,
) -> str:
    """把任务输出文件建成一个新模型库（训练产物落库的第二条路，适合放 ckpt）。

    和导出到数据集不同，这个接口是就地新建一个模型，所以 name 必须是当前账号下
    还不存在的英文名（全平台英文名唯一，重名会报 Model English name already exists）。

    task_id: 产出这些文件的任务号。
    name: 新模型库的英文名，例如 my-bert-ckpt。
    files: 要导出的文件路径列表，相对输出根，取自 openi_task_output_files 的
      name，例如 ["ckpt/model.bin"]。
    alias: 展示用的中文名，留空则与 name 相同。
    license: 许可证，例如 MIT / Apache-2.0，可留空。
    is_private: 是否私有模型库（默认 False，公开可见）。

    导出是异步的：返回里的 process_id 喂给 openi_task_export_progress 查进度。
    """
    try:
        with _client() as client:
            result = client.export_task_to_model(
                task_id,
                name,
                files,
                alias=alias,
                license=license,
                is_private=is_private,
            )
        return dump({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_task_export_progress(
    process_id: str, subject_type: str = "dataset", wait_seconds: int = 60
) -> str:
    """查任务导出进度（openi_task_export_to_dataset / _to_model 返回的 process_id）。

    subject_type: dataset 或 model，要和导出时选的那个一致。
    wait_seconds: 在这里就地轮询等待的秒数，0 表示只查一次立刻返回。导出通常
      几秒到一两分钟完成。

    返回 files 是 {文件: 百分比}：100 已完成，0 刚开始，-1 / -2 / -3 是失败
    （源文件读不到 / 目标已有同名文件 / 超出存储限额），failed 列出卖掉的文件。
    """
    try:
        with _client() as client:
            progress = client.wait_export(
                subject_type, process_id, wait_seconds=wait_seconds
            )
        return dump({"ok": True, **progress})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_list_task_templates(
    q: str = "",
    scope: str = "public",
    job_type: str = "",
    compute_source: str = "",
    tags: Optional[list[str]] = None,
    order_by: str = "",
    page: int = 1,
    page_size: int = 30,
) -> str:
    """浏览任务模板广场：别人配好的“一键开任务”模板，里面已带代码仓、分支、启动文件、
    卡型卡数、镜像和要挂载的数据集/模型。

    挑到合适的模板后直接用 openi_start_task_from_template 开任务，不需要自己找仓、
    配 spec 或查数据集 UUID。相比 openi_list_creation_options（只列规格），这里列的是
    别人跑通过的完整组合。

    q: 关键词，按模板名模糊匹配（例如 "MNIST"、"ComfyUI"、"llama"），留空返回全部。
    scope: public（平台公开的模板，约 1600 个）/ all（含私有，约 2700 个）/
      created（自己创建的）/ collected（自己收藏的）。
    job_type: DEBUG / TRAIN / INFERENCE / ONLINEINFERENCE 之一，留空不筛。
    compute_source: GPU / NPU / GCU / MLU / DCU / ILUVATAR-GPGPU / METAX-GPGPU /
      BIREN-GPU 之一，留空不筛。
    order_by: 留空用平台默认顺序，可选 newest（最新）/ usecount（被用最多）/
      collections（被收藏最多）。写别的平台会静默按默认顺序返回，所以这里直接报错。
    page / page_size: 分页，默认第 1 页、每页 30 条，上限 100。

    返回每项含 template_id、name、job_type、compute_source、acc_card_type、
    acc_cards_num、repo（要跑的代码仓）、branch、boot_file、image_*、datasets、
    models、use_count、collected。tags 是按模板自己的标签做精确匹配（大小写敏感，
    取值多为中文，如 "调试任务"、"训练任务"、"NPU"），拿不准就用 q 搜关键词。
    """
    try:
        with _client() as client:
            result = client.list_task_templates(
                scope=scope, q=q, job_type=job_type, compute_source=compute_source,
                tags=tags, order_by=order_by, page=page, page_size=page_size,
            )
        return dump({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_get_task_template(template_id: str) -> str:
    """按模板 ID 查一个任务模板的完整配置（代码仓、分支、启动文件、镜像、要挂的数据集/模型）。

    列表工具返回的字段这里都有，区别是这里能按 ID 精确取一条。注意平台只有
    GET /ai_task_template?id= 这一个按 ID 取详情的入口，列表接口的 id 参数是无效的。
    """
    try:
        with _client() as client:
            tpl = client.task_template(template_id)
        return dump({"ok": True, "template": summarize_task_template(tpl)})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_collect_task_template(template_id: str, collect: bool = True) -> str:
    """收藏 / 取消收藏一个任务模板，收藏后在 scope="collected" 里能找到。

    collect=False 表示取消收藏。
    """
    try:
        with _client() as client:
            client.collect_task_template(template_id, collect=collect)
            listed = client.list_task_templates(scope="collected", page_size=100)
        ids = [item.get("template_id") for item in listed.get("items") or []]
        return dump(
            {
                "ok": True,
                "template_id": template_id,
                "collected": collect,
                "collected_total": listed.get("total"),
                "still_collected": template_id in ids,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_start_task_from_template(
    template_id: str,
    repo: Optional[str] = None,
    display_job_name: Optional[str] = None,
    boot_file: Optional[str] = None,
    params: Optional[str] = None,
    dataset_uuid_str: Optional[str] = None,
    pretrain_model_id_str: Optional[str] = None,
    has_internet: Optional[int] = None,
    work_server_number: Optional[int] = None,
    time_limit: Optional[int] = None,
    use_template_image: bool = False,
    extra_fields: Optional[str] = None,
) -> str:
    """照着一个任务模板开计算任务：模板里的代码仓、分支、启动文件、数据集/模型挂载
    都会被带上，spec 由模板的卡型+卡数在当前账号的实时规格里配对得到。

    这是最省事的一条开任务路径：openi_list_task_templates 挑一个 template_id，
    直接传进来即可；不用自己查 spec_id、不用自己挂数据集。成功后立即开始计费
    （不足 6 分钟不扣费），用完记得 openi_stop_task。

    template_id: 来自 openi_list_task_templates / openi_get_task_template。
    repo: 代码仓 owner/name，留空用模板自带的仓（公开模板的仓多为示例仓，平台允许
      直接在上面开任务）。想跑自己的代码就填自己的仓。
    display_job_name: 任务名，留空用平台默认名（小写字母数字，3-36 位）。
    boot_file / params: 启动文件与运行参数，留空用模板里的；模板没写就留空。
    dataset_uuid_str / pretrain_model_id_str: 挂载的数据集/模型，留空用模板自带的。
      也可以写 owner/name 全名，工具会换成 UUID；写 "" 表示不挂载。
    has_internet / work_server_number / time_limit: 留空用模板里的值。
    use_template_image: 默认 False，即忽略模板里的镜像，用本工具实测能拉起的镜像
      （模板的 ImageUrl 字段有些只是内部记录 UUID，直接拿去拉会失败）。模板确实
      指定了特殊镜像（如在线推理的 openclaw-feishu-qwen3.5）时才置 True。
    extra_fields: JSON 字符串，补充平台新增字段。
    """
    try:
        extra = json.loads(extra_fields) if extra_fields else None
        with _client() as client:
            tpl = client.task_template(template_id)
            spec = client.template_spec(tpl)
            job_type = str(tpl.get("JobType") or "DEBUG")
            source = str(tpl.get("ComputeSource") or "GPU")
            cluster = str(tpl.get("Cluster") or DEFAULT_CLUSTER)
            repo = repo or f'{tpl.get("RepoOwnerName")}/{tpl.get("RepoName")}'
            boot = boot_file if boot_file is not None else (tpl.get("BootFile") or None)
            mounts = dataset_uuid_str
            if mounts is None:
                mounts = ",".join(
                    d.get("DatasetID")
                    for d in (tpl.get("DatasetLists") or [])
                    if not d.get("IsDeleted")
                )
            models = pretrain_model_id_str
            if models is None:
                models = ",".join(
                    m.get("ModelID")
                    for m in (tpl.get("ModelLists") or [])
                    if not m.get("IsDeleted")
                )
            mounts = _resolve_mount_ids(client, mounts, "dataset")
            models = _resolve_mount_ids(client, models, "model")
            image_id = image_name = image_url = None
            if use_template_image:
                image_id = tpl.get("ImageID") or None
                image_name = tpl.get("ImageName") or None
                if not image_id:
                    image_url = template_image_url(tpl)
            workers = work_server_number
            if workers is None:
                workers = int(tpl.get("AccCardsNum") or 1)
            internet = tpl.get("HasInternet") if has_internet is None else has_internet
            result = client.create_task(
                repo=repo,
                spec_id=spec.get("id"),
                display_job_name=display_job_name,
                compute_source=source,
                cluster=cluster,
                job_type=job_type,
                work_server_number=workers,
                branch_name=tpl.get("BranchName") or None,
                image_id=image_id,
                image_name=image_name,
                image_url=image_url,
                boot_file=boot,
                params=params,
                dataset_uuid_str=mounts,
                pretrain_model_id_str=models,
                has_internet=int(internet if internet is not None else 2),
                time_limit=time_limit,
                visualize_required=bool(tpl.get("VisualizeRequired")),
                extra=extra,
            )
            template_info = summarize_task_template(tpl)
        return _ok(
            result,
            ok=True,
            template={
                "template_id": template_info.get("template_id"),
                "name": template_info.get("name"),
                "repo": repo,
                "branch": template_info.get("branch"),
                "boot_file": boot,
                "job_type": job_type,
                "compute_source": source,
                "cluster": cluster,
                "acc_card_type": spec.get("acc_card_type"),
                "acc_cards_num": spec.get("acc_cards_num"),
                "spec_id": spec.get("id"),
                "unit_price": spec.get("unit_price"),
                "datasets": mounts or None,
                "models": models or None,
                "used_template_image": bool(use_template_image),
                "template_parameters": template_info.get("parameters"),
                "note": "模板里的 Parameters（表单默认值）没有自动映射到 params，需要时请自己传 params。",
            },
        )
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
    job_type: DEBUG（交互调试，可开 Jupyter/SSH）/ TRAIN（训练）/ INFERENCE（批量推理）/
      ONLINEINFERENCE（在线推理，起一个常驻服务并提供外部地址）等。
      ONLINEINFERENCE 只支持 GPU，当前账号同时只能开一个；它没有登记镜像，会走
      image_url 默认镜像；容器里的服务必须监听 8000 端口（平台不会注入端口环境变量）。
      任务 RUNNING 后用 openi_task_service_url 取对外地址。
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
def openi_stop_task(task_id: str, wait_seconds: float = 30.0) -> str:
    """停止任务并释放算力（停止后停止计费），并回读确认状态真的变成了已停止。

    平台的停止接口一受理就返回成功，任务本身还要走一段 STOPPING 才真正停。所以
    这里会轮询到状态确实变化为止，再把观测到的状态报回来。
    wait_seconds: 最长等多少秒确认状态（默认 30）。超时不会取消停止操作，只是还没
    观察到结果，返回里 ok=false 并带上 status_at_timeout——如果那时仍是 RUNNING，
    任务还在计费，需要过一会儿再用 openi_get_task 确认。
    """
    try:
        with _client() as client:
            return dump(client.task_action_verified("stop", task_id, wait_seconds=wait_seconds))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_restart_task(task_id: str, wait_seconds: float = 60.0) -> str:
    """重启一个已停止的任务，并回读确认状态真的变了。

    平台受理重启后任务会经过 QUEUED / PREPARING 再到 RUNNING，所以这里等到任务
    离开"已停止"就算确认成功（不要求等到 RUNNING，那样排队久了会超时）。
    wait_seconds: 最长等多少秒确认状态，超时只影响确认，不影响重启本身。
    """
    try:
        with _client() as client:
            return dump(client.task_action_verified("restart", task_id, wait_seconds=wait_seconds))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def openi_delete_task(task_id: str, wait_seconds: float = 30.0) -> str:
    """删除任务（不可恢复），并回读确认任务确实消失了。

    任务需先处于已停止状态，否则平台可能拒绝。删除之后查不到任务是正常结果，
    这时 ok=true。wait_seconds 超时不会取消删除，只是没观察到结果。
    """
    try:
        with _client() as client:
            return dump(client.task_action_verified("del", task_id, wait_seconds=wait_seconds))
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

    返回今天的获取流水、当天的算力 10 分是否已到账，以及规则里每一个"每日"类
    项目的完成情况（今天做了几次、还剩几次、还能拿几分、对应的工具是哪个）。
    平台的每日结算以北京时间 0 点为界。
    """
    try:
        with _client() as client:
            status = autopoint.daily_status(client)
        daily = status.get("daily_actions") or []
        reward = next((row for row in daily if row["task_code"] == autopoint.REWARD_ACTION), {})
        return dump(
            {
                "ok": True,
                **status,
                "compute_reward_points": reward.get("points_per_award", 0.0),
                "pending_daily_actions": [row for row in daily if row.get("remaining")],
                "automation": {
                    "automated_action": POINT_ACTIONS.get(autopoint.REWARD_ACTION),
                    "how": "openi_claim_daily_points 会在需要时开一个 1 分/卡时的 DEBUG 任务，"
                    "进入 RUNNING 后立即停止并删除；不足 6 分钟不计费，所以当天净赚 10 分、"
                    "算力消耗 0 分。其余每日项每条都要产出一个真实的东西（一处 commit、"
                    "一个 issue、一条评论、一个数据集），平台对重复公开项目、空评论按刷分"
                    "处理并追回积分，所以不自动刷；要做的时候用 pending_daily_actions 里"
                    "给出的工具调一次即可。",
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
