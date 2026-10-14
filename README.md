# openi-bridge

一个 MCP（Model Context Protocol）服务，用来直接使用 [OpenI 启智社区](https://openi.pcl.ac.cn)
提供的普惠算力：查询资源与积分、查看实时可开的规格（spec）与镜像、查看代码仓、
创建/停止/重启/删除计算任务、读取任务状态、日志和调试入口。

> 说明：www.openi.org.cn 只是社区门户网站，真正的算力平台是
> [openi.pcl.ac.cn](https://openi.pcl.ac.cn)（启智 AI 协作平台），本工具对接的是后者。

## 安装

~~~bash
python -m venv .venv
.venv/bin/pip install -e .
~~~

## 配置令牌

1. 打开 https://openi.pcl.ac.cn/user/settings/applications ，新建一个访问令牌（勾选需要的作用域）。
2. 设置环境变量：

~~~bash
export OPENI_TOKEN=你的令牌
# 可选，默认就是 https://openi.pcl.ac.cn
export OPENI_ENDPOINT=https://openi.pcl.ac.cn
~~~

令牌解析顺序：工具参数 → `~/.openi/token.json`（`openi login` 生成）→ 环境变量。
也可以把令牌写到 `~/.openi/token.json`：

~~~json
{ "endpoint": "https://openi.pcl.ac.cn", "token": "你的令牌" }
~~~

## 在 MCP 客户端里接入

Claude Desktop / Cursor 等支持 stdio MCP 的客户端：

~~~json
{
  "mcpServers": {
    "openi-bridge": {
      "command": "/绝对路径/openi-bridge/.venv/bin/python",
      "args": ["-m", "openi_bridge.server"],
      "env": { "OPENI_TOKEN": "你的令牌" }
    }
  }
}
~~~

Codex / 其他客户端可直接运行 `openi-bridge`（安装后生成的命令）。

## 怎么开一个任务

1. `openi_whoami` 确认令牌可用。
2. `openi_list_creation_options(job_type="DEBUG", compute_source="GPU")` 拿到实时的 `spec_id`、默认任务名和积分余额。
3. `openi_list_my_repositories` / `openi_list_repo_files` 选一个代码仓和分支。
4. `openi_start_task(repo="owner/name", spec_id=210)` 创建任务（不传镜像会用一个可拉起的默认镜像）。
5. `openi_get_task` / `openi_list_tasks` 查看状态，`openi_task_debug_url` 拿 Jupyter/SSH 入口。
6. 用完 `openi_stop_task(task_id=...)` 停止并停止计费，必要时 `openi_delete_task`。

## 镜像（重要）

不同算力类型的镜像情况不一样，都是实测出来的：

- **NVIDIA GPU**：平台给 GPU 登记的两个 `image_id` 在 C2Net 上都会以 `image not available`
  失败，`/ai_task/creation/image_by_spec` 也返回空。真正能被集群拉起的是镜像地址，可以从
  `openi_list_public_images` 拿到（平台推荐镜像的 `place` 字段），例如：

~~~text
dockerhub.pcl.ac.cn:5000/user-images/openi:SemanticSegmentEnv
swr.cn-south-1.myhuaweicloud.com/openi/cuda111_python37_pytorch191:v1
~~~

- **NPU / GCU / MLU / DCU / ILUVATAR-GPGPU / METAX-GPGPU / BIREN-GPU**：`image_id` 是好的，
  直接用 `openi_list_creation_options` 里的 `image_id`/`image_name` 即可。

不传镜像时工具会自动处理：GPU 走 `OPENI_DEFAULT_IMAGE_URL`（默认
`swr.cn-south-1.myhuaweicloud.com/openi/cuda111_python37_pytorch191:v1`，平台识别为
`cuda111_python37_pytorch191_public`）；其余算力优先用实测跑通的镜像（`src/openi_bridge/resources.py`
里的 `VERIFIED_IMAGES`，按 `spec_id` 记录），命中就填这个 `image_id`，没命中再退回平台列出的第一个
`image_id`。返回里的 `_image_source` 会说明走了哪条路径（`fallback_image_url` / `verified_image` /
`platform_default_image` / `explicit`）。

另外，除 NVIDIA GPU 外，创建任务**必须带 `branch_name`**（代码分支），否则集群会以
`Fail to load code` 失败（`CREATED_FAILED`）。工具在你不传时默认用代码仓的默认分支，`_branch_source`
会说明来源。

## 实测可用的算力

下面的结论来自逐个算力厂商开 DEBUG 任务、在容器里跑该厂商自带 SMI 的真实输出（2026-10）。
每行都标了当时实测用的任务号，可以在平台上直接查：

| 算力类型 | 实测卡型 | 验证命令 | 实测任务号 |
| --- | --- | --- | --- |
| GPU | NVIDIA Tesla V100S-PCIE-32GB（32768 MiB，驱动 570.124.06，CUDA 12.8） | `nvidia-smi` | 000000 |
| NPU | 华为昇腾 910B2（HBM 65536 MB，npu-smi 25.2.1，aarch64） | `npu-smi info` | 000000 |
| NPU | 华为昇腾 910ProA / 910A（HBM 32768 MB，npu-smi 24.1.0，aarch64） | `npu-smi info` | 000000 |
| NPU | 华为昇腾 910C（双芯，每芯 HBM 65536 MB，npu-smi 26.0.rc3） | `npu-smi info` | 000000 |
| GCU | 燧原 Enflame T20（32 G 显存，EFSMI V1.21.0，驱动 2.5.20231219） | `efsmi` | 000000 |
| MLU | 寒武纪 MLU290-M5（32768 MiB，CNMON v4.20.16） | `cnmon` | 000000 |
| DCU | 海光 DCU / K100_AI（HCU，PwrCap 400W） | `hy-smi`、`rocm-smi` | 000000 |
| ILUVATAR-GPGPU | 天数智芯 Iluvatar MR-V100（32768 MiB，IX-ML 驱动 4.4.0） | `ixsmi` | 000000 |
| BIREN-GPU | 壁仞 Biren106M（32512 MiB，400W，BR-SMI 1.10.1 / SUPA 1.10） | `brsmi` | 000000 |

每个任务的 SMI 原始输出片段保存在 [`docs/verified-outputs/`](docs/verified-outputs/)。

也就是说，除了纯 CPU，这些算力都能真正开到卡。`METAX-GPGPU`（沐曦 C500）平台上有可用规格
（`spec_id=333`），但 2026-10-13 23:00 到 2026-10-14 09:12 之间反复创建了 7 次 DEBUG 任务，
每次都卡在 `centerPending`（示例任务 000000、000000），一次都没调度到卡，所以上表里没有它；
`openi_list_creation_options(compute_source=...)` 里没有规格（spec）的只有 `CPU`。

## 提供的工具

| 工具 | 作用 |
| --- | --- |
| openi_whoami | 查看令牌对应的账号，确认令牌可用 |
| openi_list_resources | 列出集群、卡型、积分单价与计费规则（离线目录） |
| openi_list_creation_options | 列出当前账号实时可开的规格（含 spec_id）与镜像、余额 |
| openi_estimate_runtime | 按当前积分余额估算每个规格还能开多少机时（小时/天，已按 6 分钟向下取整） |
| openi_points | 查看积分余额、单日获取上限与平台公布的完整获取规则 |
| openi_points_history | 查看积分流水（获取/消耗明细，10 条一页，按天汇总） |
| openi_points_today | 看今天（北京时间）已经拿到多少分、算力那 10 分还在不在、还剩哪些每日项 |
| openi_claim_daily_points | 需要时自动跑一遍"开任务→RUNNING→停止→删除"，把当天 10 分算力积分拿到，净消耗 0 分 |
| openi_autopoint_schedule | 生成 launchd plist 与 crontab 配置，把上面这条挂成每日定时任务 |
| openi_list_images_for_spec | 查询某个 spec 可用的镜像（C2Net 上通常为空） |
| openi_list_public_images | 列出可真正拉起的公共镜像地址（平台推荐 + 自己收藏） |
| openi_list_my_repositories | 列出自己的代码仓（创建任务要指定代码仓） |
| openi_search_repositories | 搜索公开代码仓 |
| openi_list_repo_files | 列出代码仓文件与分支，用于确定启动文件 |
| openi_get_task | 按任务 ID 查询详情与状态 |
| openi_list_tasks | 列出当前账号的全部任务（跨所有代码仓） |
| openi_list_repo_tasks | 列出某个代码仓下的所有任务 |
| openi_task_logs | 读取任务日志 |
| openi_task_resource_usage | 查看任务实时算力占用 |
| openi_task_debug_url | 获取调试任务的 Jupyter / SSH 入口 |
| openi_task_visualize_url | 获取训练任务的 TensorBoard 入口 |
| openi_start_task | 新建并启动计算任务（调试/训练/推理） |
| openi_start_task_raw | 用自定义 JSON 请求体创建任务（兜底/调试用） |
| openi_stop_task | 停止任务、释放算力、停止计费 |
| openi_restart_task | 重启已停止的任务 |
| openi_delete_task | 删除任务（需先停止） |
| openi_sdk_code | 生成容器内访问数据集/模型的示例代码 |

## 计费与约束

- 消耗积分 = 单价 × 卡时 × 节点数，每 6 分钟结算一次，不足 6 分钟不计费。
- 调试（DEBUG）任务通常有单次时长上限（例如 4 小时），到期自动结束。
- 积分余额和明细见 https://openi.pcl.ac.cn/reward/point 。
- 同一作业类型同时只能有一个运行/排队中的任务（新建前先停止旧任务）。
- 任务名以小写字母或数字开头，只能包含小写字母、数字、`_` 和 `-`，不能以 `_` 结尾，最长 36 字符。

想知道余额还能开多久，直接调 `openi_estimate_runtime`：它会用实时的 `unit_price`
和当前余额，按“每 6 分钟向下取整”的口径算出每个 `spec_id` 还能开多少小时/天。
例如余额 4891.8 分时，单卡 V100（spec 210，6 分/卡时）约 815 小时，单卡 910A
（spec 328，2 分/卡时）约 2445 小时，910C（spec 392，8 分/卡时）约 611 小时。

## 获取积分的办法

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

余额、每日上限和「一次性奖励有没有领过」用 `openi_points` 看，流水用 `openi_points_history`。

## 自动获取积分

上面那张表里，只有「每日运行计算任务」这 10 分是纯算力侧的行为，可以放心自动化；
其余每日项（commit、PR、Issue、评论、建公开项目、建数据集/模型/镜像）都是仓库与社交
行为，平台明确把重复建公开项目、发空评论后删除当成刷分并追回积分，不值得为此冒险。

这 10 分可以零成本拿到，依据是两条实测结论：

- 计费每 6 分钟一档、不足 6 分钟不计费（官方文档 `docs/quickstart/resources.md`）。
  翻完账号 182 条消耗流水，1 分/卡时的卡最小的一笔正好是 0.2 分（两个计费单位），
  从来没有出现过 0.1 分，说明第一笔扣费发生在第 6 分钟。
- 一天之内多次开任务只有第一次给分。翻完 404 条获取流水，185 条
  `CreateCloudbrainTask` 分布在 185 个不同的自然日，没有任何一天拿到两次。

所以每天只要跑一个「建任务 → 等 RUNNING → 立刻停止并删除」的循环就够了，当天净赚
10 分、算力消耗 0 分；这 10 分换成最便宜的 1 分/卡时算力，正好覆盖当天 10 卡时的用量。

`openi_claim_daily_points` 就是这个循环，也可以直接在命令行跑：

~~~bash
python -m openi_bridge.autopoint --status    # 只看今天拿了多少分
python -m openi_bridge.autopoint --dry-run   # 打印计划，不动算力
python -m openi_bridge.autopoint             # 没拿到就补一次
python -m openi_bridge.autopoint --reap      # 回收遗留的 autopoint-* 任务
~~~

默认用已实测能拉起的 1 分/卡时规格（燧原 T20 spec 229、寒武纪 MLU290 spec 184、
天数 MR-V100 spec 388、壁仞 BIREN106M spec 347），代码仓取账号下第一个非空仓库，
可用 `--repo` / `--source` 或环境变量 `OPENI_AUTOPOINT_REPO` / `OPENI_AUTOPOINT_SOURCE`
指定。排队和 `PREPARING` 不计费，所以等 RUNNING 可以等久一点（默认 900 秒）；
一旦发现 RUNNING 就立即停止，实际运行只有几秒。脚本带兜底：无论失败还是被中断，
都会在 `finally` 里停掉并删除刚建的任务。

挂成每日定时任务（`openi_autopoint_schedule` 会直接返回下面这些内容）：

~~~bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/cn.openi.autopoint.plist
# 或
crontab -e
5 9 * * * cd /path/to/openi-bridge && .venv/bin/python -m openi_bridge.autopoint --json >> ~/.openi/autopoint.log 2>&1
*/30 * * * * cd /path/to/openi-bridge && .venv/bin/python -m openi_bridge.autopoint --reap >> ~/.openi/autopoint.log 2>&1
~~~

第二条是兜底：万一某次跑崩了，半小时内也会把 `autopoint-*` 任务停掉删掉。

## 实现说明与已知限制

本工具基于平台前端实际调用的 REST 接口（`/api/v1/...`）实现，创建任务的请求体取自平台源码 `entity.CreateReq`。已用真实令牌验证过的接口与行为：

- 账号与代码仓：`/user`、`/user/repos`、`/repos/{owner}/{repo}`、`/branches`、`/contents`。
- 资源发现：`/ai_task/creation/required` 返回当前账号在指定 `job_type` + `compute_source` + `cluster_type` 下真实可用的规格列表（含数字 `spec_id`、卡型、卡数、CPU/内存、单价）、可用镜像、默认任务名、积分余额，以及 `can_create_more` / `not_stop_task_count` 等配额信息。
- 镜像目录：`/images/recommend`（平台推荐）与 `/images/star`（本账号收藏），其中的 `place` 才是集群实际拉取的镜像地址。
- 任务读写：`/ai_task`、`/ai_task/brief`、`/ai_task/log`、`/ai_task/resource_usage`、`/ai_task/debug_url`、`/ai_task/visualize_url`、`/ai_task/my_list`（账号级任务列表）、`/{owner}/{repo}/ai_task`（按仓列表）。
- 任务变更：`POST /{owner}/{repo}/ai_task/create`、`/ai_task/stop`、`/ai_task/restart`、`/ai_task/del`、`/ai_task/batch_del`。

需要注意：

- 普通用户可用的集群是 `C2Net`（智算网络）。`cluster` 必须填 `C2Net`，`cluster_type` 查询参数同理；`openi` 之类的值会返回参数错误或空规格。
- 创建任务的必填字段是 `job_type`、`display_job_name`、`spec_id`（整数）、`compute_source`、`cluster`；`spec_id` 必须来自 `openi_list_creation_options`，否则会返回 Specification not available。
- 创建是异步的：`create` 立即返回任务 ID（状态 `PREPARING`），随后集群异步拉起。如果集群侧排队或拉起失败，任务会进入失败态，且不计费；用 `openi_get_task` 观察状态。
- DEBUG 任务不需要 `boot_file`；训练类任务需要以 `.py` 结尾的 `boot_file`。
- 账号级任务列表用的是 `/ai_task/my_list`，它在平台前端真实存在，但字段较少；更完整的信息用 `openi_get_task`。
- 镜像：只有 NVIDIA GPU 的登记 `image_id` 在 C2Net 上拉不起来（`image not available`），工具对 GPU 会改用 `OPENI_DEFAULT_IMAGE_URL` 并把镜像地址作为 `image_url` 传下去；其余算力的 `image_id` 可直接使用。
- `branch_name`：除 NVIDIA GPU 外的算力在创建时都需要它，缺失会以 `Fail to load code`（`CREATED_FAILED`）失败；工具默认取代码仓的默认分支。
- 可用性：`GPU` / `NPU` / `GCU` / `MLU` / `DCU` / `ILUVATAR-GPGPU` / `BIREN-GPU` 都已开卡验证（见上面的“实测可用的算力”）；`METAX-GPGPU` 有规格但实测时集群排队未跑到卡；`CPU` 是唯一拿不到规格的 `compute_source`。

