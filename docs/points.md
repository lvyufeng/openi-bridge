# 积分


平台公布的规则和上限（`openi_points` 直接返回，来自 `/reward/point/rule/config`）：
单日获取上限 **100 分**，打榜/活动奖励另行计算、不占这个额度。按天结算的主要是这几项：

| 行为 | 分/次 | 每日次数 | 每日上限 |
| --- | --- | --- | --- |
| 邀请好友注册 | 10 | 10 | 100 |
| 每日运行计算任务（调试/训练/推理/评测任一） | 10 | 1 | 10 |
| 数据集 / 模型 / 镜像被平台推荐 | 5 | 3 | 各 15 |
| 每日 commit | 1 | 3 | 3 |
| 创建或 Fork 公开项目 | 1 | 3 | 3 |
| 每日提出 PR / Issue | 1 | 3 | 各 3 |
| 创建数据集 / 模型 / 公开镜像 / 迁移外部模型 | 1 | 3 | 各 3 |
| 发表评论 | 1 | 2 | 2 |
| 上传数据集附件 | 1 | 1 | 1 |
| 首次更换头像 | 2 | 一次性 | 2 |
| 完成微信扫码验证 | 50 | 一次性 | 50 |

把每日项全部做满理论上是 182 分/天，但会被 100 分的单日上限截断。不算邀请好友的话，
每天仍然有约 82 分可拿。因为最便宜的算力是 1 分/卡时，只靠“每日运行计算任务”的 10 分
就够每天开 10 卡时（燧原 T20、寒武纪 MLU290、天数 MR-V100、壁仞 BIREN106M 等）。
历史上积分的大头来自打榜活动和“众智开发支持”这类人工发放，单笔 1000~3000 分。

余额、每日上限和「一次性奖励有没有领过」用 `openi_points` 看，流水用
`openi_points_history`，今天拿了多少用 `openi_points_today`。

### 自动获取积分

上面那张表里，只有「每日运行计算任务」这 10 分是纯算力侧的行为，可以放心自动化；其余
每日项（commit、PR、Issue、评论、建公开项目、建数据集/模型/镜像）都得真的产出一个东西，
平台明确把重复建公开项目、发空评论后删除当成刷分并追回积分，所以不自动刷。但每一项都
有对应的工具，想做的时候一次调用就能完成：`openi_points_today` 会列出今天每个每日项
做了几次、还剩几次、还能拿几分、以及用哪个工具——commit 用 `openi_push_repo_files` /
`openi_write_repo_file`，建公开项目用 `openi_create_repository`，Issue 用
`openi_create_repo_issue`，评论用 `openi_comment_repo_issue`，建数据集/模型用
`openi_create_dataset` / `openi_create_model`。

这 10 分可以零成本拿到，依据是两条实测结论：

- 计费每 6 分钟一档、不足 6 分钟不计费（官方文档 `docs/quickstart/resources.md`）。翻完
  账号 182 条消耗流水，1 分/卡时的卡最小的一笔正好是 0.2 分（两个计费单位），从来没有
  出现过 0.1 分，说明第一笔扣费发生在第 6 分钟。
- 一天之内多次开任务只有第一次给分。翻完 404 条获取流水，185 条 `CreateCloudbrainTask`
  分布在 185 个不同的自然日，没有任何一天拿到两次。

所以每天只要跑一个「建任务 → 等 RUNNING → 立刻停止并删除」的循环就够了，当天净赚
10 分、算力消耗 0 分；这 10 分换成最便宜的 1 分/卡时算力，正好覆盖当天 10 卡时的用量。

`openi_claim_daily_points` 就是这个循环，也可以直接在命令行跑：

~~~bash
python -m openi_bridge.autopoint --status    # 只看今天拿了多少分
python -m openi_bridge.autopoint --daily     # 列出每个每日项还剩多少分、用哪个工具做
python -m openi_bridge.autopoint --dry-run   # 打印计划，不动算力
python -m openi_bridge.autopoint             # 没拿到就补一次
python -m openi_bridge.autopoint --reap      # 回收遗留的 autopoint-* 任务
~~~

默认用已实测能拉起的 1 分/卡时规格（燧原 T20 spec 229、寒武纪 MLU290 spec 184、
天数 MR-V100 spec 388、壁仞 BIREN106M spec 347），代码仓取账号下第一个非空仓库，可用
`--repo` / `--source` 或环境变量 `OPENI_AUTOPOINT_REPO` / `OPENI_AUTOPOINT_SOURCE` 指定。
排队和 `PREPARING` 不计费，所以等 RUNNING 可以等久一点（默认 900 秒）；一旦发现 RUNNING
就立即停止，实际运行只有几秒。脚本带兜底：无论失败还是被中断，都会在 `finally` 里停掉
并删除刚建的任务。

挂成每日定时任务（`openi_autopoint_schedule` 会直接返回下面这些内容）：

~~~bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/cn.openi.autopoint.plist
# 或
crontab -e
5 9 * * * cd /path/to/openi-bridge && .venv/bin/python -m openi_bridge.autopoint --json >> ~/.openi/autopoint.log 2>&1
*/30 * * * * cd /path/to/openi-bridge && .venv/bin/python -m openi_bridge.autopoint --reap >> ~/.openi/autopoint.log 2>&1
~~~

第二条是兜底：万一某次跑崩了，半小时内也会把 `autopoint-*` 任务停掉删掉。
