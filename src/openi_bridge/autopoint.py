"""每天自动领取算力积分（零净成本）。

平台公布的获取规则里（/reward/point/rule/config），唯一和算力直接相关、又能每天
重复拿的只有一条：

    CreateCloudbrainTask  每日运行计算任务  10 分 / 天 / 1 次

其余"每日"类积分项（commit、建公开项目、提 Issue、评论、建数据集/模型、上传附件）
都能通过本工具的其它 MCP 工具做到，但都要求你确实产出一个东西，规则里每条又有
每日次数上限，平台明确把重复建公开项目、发空评论后再删当成刷分并追回积分。
所以 autopoint 只自动化那条零成本的算力积分，别的项只做清点：daily_actions()
会列出今天每项还能拿几分、对应哪个工具，要做的时候一次调用就能完成。

计费规则是每 0.1 小时（6 分钟）结算一次、不足 6 分钟不计费，官方文档见
https://openi.pcl.ac.cn/docs/quickstart/resources.md 。实测的账户消耗流水里，
1 分/卡时的卡最小的一笔是 0.2 分（= 2 个计费单位），从未出现过 0.1 分，
说明第一笔扣费发生在第 6 分钟。

于是"开一个任务 → 进入 RUNNING → 立刻停止并删除"这条循环当天净赚 10 分、
消耗 0 分。平台上一天内多次开任务只有第一次给分（翻遍 404 条获取流水，没有
任何一天拿到两次 CreateCloudbrainTask），所以每天跑一次就够，不需要刷量。

用法：

    python -m openi_bridge.autopoint             # 今天没拿就补一次
    python -m openi_bridge.autopoint --status    # 只看今天拿了多少分
    python -m openi_bridge.autopoint --dry-run   # 打印将要做什么，不动算力
    python -m openi_bridge.autopoint --reap      # 只回收遗留的 autopoint-* 任务

相关环境变量：

    OPENI_TOKEN                令牌，见 README
    OPENI_AUTOPOINT_REPO       任务使用的代码仓 owner/name，默认取账号下第一个非空仓库
    OPENI_AUTOPOINT_SOURCE     指定算力类型（GCU/MLU/...），默认挑最便宜的
    OPENI_AUTOPOINT_TIMEOUT    等任务进入 RUNNING 的最长秒数，默认 900

只有任务真正 RUNNING（卡被占用）的那段时间计费，排队和 PREPARING 不计费，所以
可以放心把排队时间等得久一点；一旦发现 RUNNING 就立刻停止，运行时间远小于
6 分钟，因此不产生扣费。脚本自己带兜底：无论中途失败还是被中断，都会在 finally
里停掉并删除刚建的任务，`--reap` 还能回收上一次意外留下的任务。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from .client import COMPUTE_SOURCES as ALL_SOURCES
from .client import DEFAULT_CLUSTER, OpenIClient, dump
from .resources import POINT_ACTIONS

BEIJING = timezone(timedelta(hours=8))

# 每天只给一次的算力积分项。
REWARD_ACTION = "CreateCloudbrainTask"

# 每个"每日"类积分项对应到能产生它的 MCP 工具（空字符串表示这条 MCP 覆盖不到，
# 只能去平台网页上做，比如邀请好友、被平台推荐、提交镜像）。
DAILY_ACTION_TOOLS: Dict[str, str] = {
    "PushCommits": "openi_push_repo_files / openi_write_repo_file",
    "CreatePublicRepo": "openi_create_repository",
    "CreateIssue": "openi_create_repo_issue",
    "CommentIssue": "openi_comment_repo_issue",
    "TaskCreateDataset": "openi_create_dataset",
    "TaskCreateAimodel": "openi_create_model",
    "CreatePullRequest": "",
    "CreateNewModelTask": "",
    "CreateImage": "",
    "UploadAttachment": "",
    "TaskInviteFriendRegister": "",
    "DatasetRecommended": "",
    "TaskAimodelRecommended": "",
    "ImageRecommend": "",
}

# 计费窗口：不足 6 分钟不计费（官方文档 + 消耗流水双重验证）。
FREE_WINDOW_SECONDS = 6 * 60

# 自动化创建的任务统一用这个前缀命名，方便只回收自己留下的任务。
TASK_NAME_PREFIX = "autopoint"

# 在卡上跑代码（exec_on_card）建的任务用这个前缀，回收时一并处理。
EXEC_TASK_PREFIX = "openixec"

# 1 分/卡时的规格，按"已经用真实镜像在 C2Net 上拉起过"的顺序优先尝试。
PREFERRED_SPECS: Sequence[tuple[str, int]] = (
    ("GCU", 229),  # ENFLAME-T20
    ("MLU", 184),  # MLU290
    ("ILUVATAR-GPGPU", 388),  # MR-V100
    ("BIREN-GPU", 347),  # BIREN106M
)

RUNNING_STATUSES = {"RUNNING", "RUNNING_OR_STOPPED"}
ENDED_STATUSES = {
    "STOPPED",
    "FINISHED",
    "SUCCEEDED",
    "FAILED",
    "CREATED_FAILED",
    "CANCELLED",
}


# --------------------------------------------------------------------- helpers


def beijing_day(ts: Optional[Any] = None) -> str:
    """流水里的 LastOperateDate 是 unix 秒，按北京时间分野。"""
    if ts in (None, ""):
        return datetime.now(BEIJING).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(int(ts), BEIJING).strftime("%Y-%m-%d")


def _increase_records(
    client: OpenIClient, day: Optional[str] = None, max_pages: int = 20
) -> List[Dict[str, Any]]:
    """翻获取流水，只留下指定日期（默认今天）的记录。

    接口按时间倒序返回，10 条一页，所以翻到比目标日期更早的一页就可以停。
    """
    day = day or beijing_day()
    kept: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        data = client.points_records("INCREASE", page)
        batch = (data or {}).get("Records") or []
        if not batch:
            break
        stop = False
        for rec in batch:
            ts = rec.get("LastOperateDate")
            if not ts:
                continue
            if beijing_day(ts) == day:
                kept.append(rec)
            elif beijing_day(ts) < day:
                stop = True
        if stop:
            break
    return kept


def _daily_actions(
    rules: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """把"每日"类规则和今天的流水对起来，算每项今天做了几次、还剩几次。

    流水里的 SourceTemplateId 就是规则里的 TaskCode。每项都带 daily_limit，
    所以只有第一笔会到账（翻历史流水没有任何一天超出过一次），done_today 按
    上限截断，避免重复调用时把次数算虚。
    """
    counts: Dict[str, int] = {}
    for rec in records:
        code = rec.get("SourceTemplateId") or rec.get("SourceType")
        if code:
            counts[code] = counts.get(code, 0) + 1
    out: List[Dict[str, Any]] = []
    for rule in rules:
        if rule.get("RefreshRate") != "DAILY":
            continue
        code = rule.get("TaskCode")
        amount = float(rule.get("AwardAmount") or 0)
        limit = int(rule.get("LimitNum") or 0)
        done = min(counts.get(code, 0), limit)
        remaining = max(0, limit - done)
        out.append(
            {
                "task_code": code,
                "action": POINT_ACTIONS.get(code, code),
                "points_per_award": amount,
                "daily_limit": limit,
                "done_today": done,
                "remaining": remaining,
                "points_remaining": amount * remaining,
                "tool": DAILY_ACTION_TOOLS.get(code, ""),
            }
        )
    out.sort(key=lambda row: (-row["points_remaining"], row["task_code"]))
    return out


def daily_actions(client: OpenIClient, day: Optional[str] = None) -> List[Dict[str, Any]]:
    """今天每个"每日"积分项做了几次、还能拿多少分、用哪个工具去做。"""
    rules = (client.points_rules() or {}).get("TaskRules") or []
    return _daily_actions(rules, _increase_records(client, day))


def daily_status(client: OpenIClient, day: Optional[str] = None) -> Dict[str, Any]:
    """今天已经拿了多少分、算力那 10 分有没有拿到，以及每个每日项还剩多少。"""
    day = day or beijing_day()
    account = client.points_account()
    records = _increase_records(client, day)
    points = sum(float(r.get("Amount") or 0) for r in records)
    claimed = any((r.get("SourceTemplateId") or r.get("SourceType")) == REWARD_ACTION for r in records)
    config = client.points_rules() or {}
    actions = _daily_actions(config.get("TaskRules") or [], records)
    return {
        "day": day,
        "balance": account.get("Balance"),
        "total_earned": account.get("TotalEarned"),
        "total_consumed": account.get("TotalConsumed"),
        "today_points": points,
        "today_actions": sorted({(r.get("SourceTemplateId") or r.get("SourceType")) for r in records}),
        "compute_reward_claimed": claimed,
        "compute_reward_missing": not claimed,
        "daily_actions": actions,
        "daily_points_remaining": sum(row["points_remaining"] for row in actions),
    }


def _active_tasks(client: OpenIClient) -> List[Dict[str, Any]]:
    """账号下还没结束的任务（含排队中）。"""
    data = client.list_tasks(page=1, page_size=50)
    rows = data.get("tasks") if isinstance(data, dict) else data
    out = []
    for item in rows or []:
        task = item.get("task") if isinstance(item, dict) and "task" in item else item
        if not isinstance(task, dict):
            continue
        if (task.get("status") or "").upper() not in ENDED_STATUSES:
            out.append(task)
    return out


def _stop_and_delete(client: OpenIClient, task_id: Any) -> Dict[str, Any]:
    """停止并删除一个任务，两步都尽力而为地报告结果。"""
    report: Dict[str, Any] = {"task_id": task_id}
    try:
        client.task_action("stop", task_id)
        report["stopped"] = True
    except Exception as exc:  # noqa: BLE001
        report["stopped"] = False
        report["stop_error"] = str(exc)
    for _ in range(6):
        try:
            brief = client.task_brief(task_id)
        except Exception:  # noqa: BLE001
            break
        if (brief.get("status") or "").upper() in ENDED_STATUSES:
            break
        time.sleep(3)
    try:
        client.task_action("del", task_id)
        report["deleted"] = True
    except Exception as exc:  # noqa: BLE001
        report["deleted"] = False
        report["delete_error"] = str(exc)
    return report


def reap_leftovers(client: OpenIClient) -> List[Dict[str, Any]]:
    """回收上一次自动化留下的、没被及时停掉的任务。"""
    reaped = []
    for task in _active_tasks(client):
        name = (task.get("display_job_name") or "")
        if name.startswith(TASK_NAME_PREFIX) or name.startswith(EXEC_TASK_PREFIX):
            reaped.append(_stop_and_delete(client, task.get("id")))
    return reaped


def _cheapest_spec(
    client: OpenIClient, job_type: str, compute_source: Optional[str] = None
) -> Dict[str, Any]:
    """挑出单价最低的规格；优先用已经实测能拉起来的 1 分/卡时规格。"""
    if compute_source:
        candidates: List[tuple[Optional[str], Optional[int]]] = [(compute_source, None)]
    else:
        candidates = [(source, spec_id) for source, spec_id in PREFERRED_SPECS]
        # 兜底：所有算力类型里逐个问，挑单价最低的那个。
        candidates += [(source, None) for source in ALL_SOURCES]
    best_any: Optional[Dict[str, Any]] = None
    for source, spec_id in candidates:
        try:
            info = client.creation_info(job_type, source)
            specs = (info.get("specs") or {}).get("all") or []
            if spec_id is not None:
                best = next((s for s in specs if int(s.get("id") or 0) == int(spec_id)), None)
            else:
                priced = [s for s in specs if s.get("unit_price") is not None]
                best = min(priced, key=lambda s: float(s.get("unit_price") or 0)) if priced else None
            if not best:
                continue
            picked = {
                "spec_id": best.get("id"),
                "compute_source": best.get("compute_resource") or source,
                "card_type": best.get("acc_card_type"),
                "price_per_card_hour": best.get("unit_price"),
                "cluster": DEFAULT_CLUSTER,
                "job_type": job_type,
            }
            # 优先规格命中就走这个；否则记下当前最便宜的再去试下一类。
            if spec_id is not None:
                if not info.get("can_create_more", True):
                    return {
                        "blocked": True,
                        "reason": "平台报告不能再创建任务（同时只能有 1 个）",
                        "info": info,
                    }
                return picked
            if best_any is None or float(picked["price_per_card_hour"] or 0) < float(
                best_any["price_per_card_hour"] or 0
            ):
                best_any = picked
        except Exception:  # noqa: BLE001
            continue
    if best_any is not None:
        return best_any
    return {"blocked": True, "reason": "没有找到可用的规格"}


def _pick_repo(client: OpenIClient, repo: Optional[str] = None) -> Optional[str]:
    if repo:
        return repo
    env = os.getenv("OPENI_AUTOPOINT_REPO")
    if env:
        return env
    try:
        repos = client.my_repos(limit=50)
    except Exception:  # noqa: BLE001
        return None
    for item in repos or []:
        if item.get("empty"):
            continue
        if item.get("mirror"):
            continue
        return item.get("full_name") or item.get("name")
    for item in repos or []:
        return item.get("full_name") or item.get("name")
    return None


# ----------------------------------------------------------------------- loop


def launch_and_wait(
    client: OpenIClient,
    repo: str,
    spec: Dict[str, Any],
    wait_seconds: float,
    name_prefix: str = TASK_NAME_PREFIX,
    job_type: str = "DEBUG",
) -> Dict[str, Any]:
    """建任务并等它进入 RUNNING。

    调用方负责清理（正常路径和异常路径都要停删）。排队与 PREPARING 不计费，
    所以等待时间可以给得比 6 分钟长；一旦 RUNNING 就应该尽快停掉。
    """
    started = time.monotonic()
    name = f"{name_prefix}-{datetime.now(BEIJING).strftime('%H%M%S')}"
    created = client.create_task(
        repo=repo,
        spec_id=spec["spec_id"],
        compute_source=spec["compute_source"],
        cluster=spec["cluster"],
        job_type=job_type,
        display_job_name=name,
    )
    task_id = created.get("id") or created.get("task_id") or created.get("Id")
    report: Dict[str, Any] = {
        "task_id": task_id,
        "task_name": name,
        "create_response": created,
        "reached_running": False,
        "status": None,
    }
    if not task_id:
        report["error"] = "创建任务后没有拿到任务 ID"
        return report
    deadline = started + wait_seconds
    while time.monotonic() < deadline:
        brief = client.task_brief(task_id)
        status = (brief.get("status") or "").upper()
        report["status"] = status
        if status in RUNNING_STATUSES:
            report["reached_running"] = True
            break
        if status in ENDED_STATUSES:
            break
        time.sleep(3)
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def claim_daily_points(
    client: OpenIClient,
    repo: Optional[str] = None,
    job_type: str = "DEBUG",
    compute_source: Optional[str] = None,
    timeout: Optional[float] = None,
    dry_run: bool = False,
    keep_seconds: float = 0.0,
    force: bool = False,
) -> Dict[str, Any]:
    """跑一遍"开任务 → RUNNING → 停止 → 删除"，拿当天的 10 分。

    返回一个可序列化的报告；`changed` 表示这次是否真的动了算力。
    """
    compute_source = compute_source or os.getenv("OPENI_AUTOPOINT_SOURCE") or None
    if timeout is None:
        timeout = float(os.getenv("OPENI_AUTOPOINT_TIMEOUT") or 900)

    status = daily_status(client)
    report: Dict[str, Any] = {"status_before": status, "changed": False}
    if status["compute_reward_claimed"] and not force:
        report.update(
            {
                "ok": True,
                "action": "skip",
                "reason": f"今天（{status['day']}）的“每日运行计算任务”10 分已经拿到了",
            }
        )
        return report

    reaped = reap_leftovers(client)
    if reaped:
        report["reaped"] = reaped

    repo = _pick_repo(client, repo)
    if not repo:
        report.update({"ok": False, "action": "error", "reason": "找不到可用的代码仓来建任务"})
        return report

    spec = _cheapest_spec(client, job_type, compute_source)
    if spec.get("blocked"):
        report.update({"ok": False, "action": "blocked", "reason": spec.get("reason")})
        return report
    report["spec"] = spec
    report["repo"] = repo

    if dry_run:
        report.update({"ok": True, "action": "dry_run", "planned": {"repo": repo, **spec}})
        return report

    started = time.monotonic()
    # 排队与 PREPARING 不计费（账单里的时长 = start_time..end_time），所以这里
    # 只限制等 RUNNING 的时间，不限制总时长；RUNNING 之后马上停，运行远小于 6 分钟。
    deadline = started + timeout
    task_id: Optional[Any] = None
    outcome: Dict[str, Any] = {}

    try:
        name = f"{TASK_NAME_PREFIX}-{datetime.now(BEIJING).strftime('%H%M%S')}"
        created = client.create_task(
            repo=repo,
            spec_id=spec["spec_id"],
            compute_source=spec["compute_source"],
            cluster=spec["cluster"],
            job_type=job_type,
            display_job_name=name,
        )
        task_id = created.get("id") or created.get("task_id") or created.get("Id")
        report["task_id"] = task_id
        report["create_response"] = created
        if not task_id:
            report.update({"ok": False, "action": "error", "reason": "创建任务后没有拿到任务 ID"})
            return report

        reached_running = False
        last_status = None
        while time.monotonic() < deadline:
            brief = client.task_brief(task_id)
            last_status = (brief.get("status") or "").upper()
            if last_status in RUNNING_STATUSES:
                reached_running = True
                break
            if last_status in ENDED_STATUSES:
                break
            time.sleep(3)
        report["reached_running"] = reached_running
        report["status_at_create"] = last_status

        if reached_running and keep_seconds > 0:
            remaining = FREE_WINDOW_SECONDS - 45 - (time.monotonic() - started)
            time.sleep(max(0.0, min(keep_seconds, remaining)))
        outcome = _stop_and_delete(client, task_id)
        report["cleanup"] = outcome
        report["ran_seconds"] = round(time.monotonic() - started, 1)
    except Exception as exc:  # noqa: BLE001
        report.update({"ok": False, "action": "error", "reason": str(exc)})
        if task_id:
            outcome = _stop_and_delete(client, task_id)
            report["cleanup"] = outcome
        return report
    finally:
        # 兜底：任何路径都不能把任务留在运行态。
        if task_id and not outcome:
            try:
                report["cleanup"] = _stop_and_delete(client, task_id)
            except Exception as exc:  # noqa: BLE001
                report["cleanup_error"] = str(exc)

    report["status_after"] = daily_status(client)
    report["changed"] = True
    report["ok"] = bool(outcome.get("deleted"))
    if not report["ok"]:
        report["action"] = "partial"
    elif not report.get("reached_running"):
        report["action"] = "timeout"
    else:
        report["action"] = "claimed"
    if not report["ok"]:
        report["reason"] = "任务没能完全清理，请检查 report.cleanup"
    elif report["action"] == "timeout":
        report["reason"] = (
            f"任务在 {timeout:.0f} 秒内没进入 RUNNING（集群排队），已停止并删除；"
            "那 10 分可能还没到账，下次运行会自动重试"
        )
    return report


# ------------------------------------------------------------------------ CLI


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m openi_bridge.autopoint",
        description="自动领取 OpenI 每日算力积分（开任务不足 6 分钟不扣费）。",
    )
    parser.add_argument("--status", action="store_true", help="只打印今天的积分状态")
    parser.add_argument(
        "--daily", action="store_true", help="只打印今天每个每日积分项还剩多少、用哪个工具做"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不创建任务")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--repo", default=None, help="任务使用的代码仓 owner/name")
    parser.add_argument("--source", default=None, help="指定算力类型，如 GCU/MLU/ILUVATAR-GPGPU")
    parser.add_argument("--job-type", default="DEBUG", help="任务类型，默认 DEBUG")
    parser.add_argument("--timeout", type=float, default=None, help="等 RUNNING 的最长秒数")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使今天已经拿到那 10 分也照常跑一遍，用来验证链路是否可用",
    )
    parser.add_argument("--reap", action="store_true", help="只回收遗留的 autopoint-* 任务")
    args = parser.parse_args(argv)

    with OpenIClient() as client:
        if args.reap:
            reaped = reap_leftovers(client)
            report = {"ok": True, "action": "reap", "reaped": reaped, "status": daily_status(client)}
        elif args.daily:
            report = {"ok": True, **daily_status(client), "action": "daily_plan"}
        elif args.status:
            report = daily_status(client)
        else:
            report = claim_daily_points(
                client,
                repo=args.repo,
                job_type=args.job_type,
                compute_source=args.source,
                timeout=args.timeout,
                dry_run=args.dry_run,
                force=args.force,
            )

    if args.json:
        print(dump(report))
    else:
        _print_human(report)
    return 0 if report.get("ok", True) else 1


def _print_human(report: Dict[str, Any]) -> None:
    if report.get("action") == "reap":
        reaped = report.get("reaped") or []
        print(f"回收        {len(reaped)} 个遗留任务 {[r.get('task_id') for r in reaped]}")
        status = report.get("status") or {}
    else:
        status = report.get("status_before") or report
    print(f"日期        {status.get('day')}")
    print(f"余额        {status.get('balance')} 分（累计赚 {status.get('total_earned')}，已消耗 {status.get('total_consumed')}）")
    print(f"今天已得    {status.get('today_points')} 分  明细 {status.get('today_actions')}")
    print(f"算力那 10 分 {'已拿到' if status.get('compute_reward_claimed') else '还没拿到'}")
    remaining = [row for row in (status.get("daily_actions") or []) if row.get("remaining")]
    if remaining:
        print(f"每日项      还能拿 {status.get('daily_points_remaining')} 分")
        for row in remaining:
            tool = f"   用 {row['tool']}" if row.get("tool") else ""
            print(
                f"            {row['action']}  {row['points_per_award']} 分 ×{row['remaining']}"
                f" = {row['points_remaining']} 分{tool}"
            )
    if report.get("action") == "skip":
        print(f"结论        跳过：{report.get('reason')}")
        return
    if report.get("action") == "dry_run":
        print(f"结论        演练：计划用 {report['planned']}")
        return
    if report.get("action") == "daily_plan":
        if not remaining:
            print("结论        今天的每日项已经全部拿满")
        return
    if "task_id" in report:
        print(f"任务        #{report['task_id']}  spec {report.get('spec', {}).get('spec_id')} "
              f"{report.get('spec', {}).get('card_type')}  进入 RUNNING={report.get('reached_running')}  "
              f"用时 {report.get('ran_seconds')}s")
        print(f"清理        {report.get('cleanup')}")
    after = report.get("status_after")
    if after:
        print(f"跑完后      今天 {after.get('today_points')} 分，算力那 10 分"
              f"{'已拿到' if after.get('compute_reward_claimed') else '仍未到账'}")
    if "task_id" in report or report.get("action") in (
        "skip",
        "dry_run",
        "daily_plan",
        "blocked",
        "error",
        "partial",
        "claimed",
        "timeout",
    ):
        print(f"结论        {report.get('action')}：{report.get('reason') or '完成'}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
