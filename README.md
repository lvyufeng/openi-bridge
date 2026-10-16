# openi-bridge

一个 MCP（Model Context Protocol）服务，让你在 Claude Code / Codex 里用一句话就用上
[OpenI 启智社区](https://openi.pcl.ac.cn)的普惠算力：查余额和积分、看当前能开哪些规格
（spec）和镜像、选代码仓、创建/停止/重启/删除计算任务、读任务状态与日志，以及直接在
云脑的一张卡上跑一段代码。也可以就地创建数据集和模型库，把本地文件（数据集、ckpt
权重等）直接传上去，再把返回的 UUID 挂到计算任务里。代码仓也一并管起来：建仓、读写
文件、看整仓文件树与提交历史、打包下载、提 issue 与评论。
任务跑完还能把产出收回来：列输出目录、下载输出文件、拉整份日志，任务排队或失败时看
调度事件时间线与节点，训练任务可以读 loss 曲线。

> www.openi.org.cn 只是社区门户网站，真正的算力平台是
> [openi.pcl.ac.cn](https://openi.pcl.ac.cn)（启智 AI 协作平台），本工具对接的是后者。

## 目录

- [快速开始](#快速开始)：几分钟装好，任意目录可用
- [怎么用](#怎么用)：自然语言用法、常用工具、命令行
- [配置令牌](#配置令牌)
- [算力与镜像](#算力与镜像)：哪些卡能开、镜像怎么选
- [计费与约束](#计费与约束)
- [积分](#积分)：领取规则与每日自动领分
- [在卡上跑代码](#在卡上跑代码)
- [工具全表](#工具全表)
- [实现说明与已知限制](#实现说明与已知限制)

## 快速开始

目标：在**任意目录**下打开 Claude Code 或 Codex，直接说人话就能开卡，不用先
`cd` 到某个仓库、也不用设环境变量。

**第 0 步，拿一个令牌。** 打开 https://openi.pcl.ac.cn/user/settings/applications
新建访问令牌（勾选需要的作用域），然后写进 `~/.openi/token.json`：

~~~json
{ "endpoint": "https://openi.pcl.ac.cn", "token": "你的令牌" }
~~~

**第 1 步，装一份不依赖当前目录的运行时。** 用一份固定放在家目录下的独立 venv
（editable 装法，改代码即生效）：

~~~bash
python3 -m venv ~/.local/share/openi-bridge/venv
~/.local/share/openi-bridge/venv/bin/pip install -e /path/to/openi-bridge
~~~

把 `/path/to/openi-bridge` 换成这个仓库的绝对路径。

**第 2 步，注册到 Claude Code 和 Codex。** 仓库自带的 `openi-mcp-install` 会把启动
命令算成绝对路径，同时写进两个客户端：

~~~bash
# 先干跑一遍，看看会写什么、会执行什么命令
~/.local/share/openi-bridge/venv/bin/python -m openi_bridge.install_mcp
# 确认无误再加 --apply 真正写入
~/.local/share/openi-bridge/venv/bin/python -m openi_bridge.install_mcp --apply
~~~

它会往 Codex 的 `config.toml` 写一段 `[mcp_servers.openi-bridge]`（改前自动备份成
`config.toml.bak`），并执行 `claude mcp add --scope user openi-bridge <绝对路径>`。
Codex 读的是 `$CODEX_HOME/config.toml`（没设就是 `~/.codex/config.toml`），这台机器上
`CODEX_HOME` 指向 `~/.codex-tui`，同时又有一份 `~/.codex/config.toml`，所以两份都会写到，
换个客户端也找得到。两处都只放**启动命令**，不放令牌——运行时从 `~/.openi/token.json`
读，所以配置里没有明文密钥，换令牌也不用改配置。

**第 3 步，重启客户端并验证。** 用 `claude mcp list` / `codex mcp list` 看是否
Connected / enabled（Claude Code 里也可以 `/mcp` 查看），然后随便找个目录问一句
“OpenI 上现在有哪些卡能开、我还剩多少积分”。想确认令牌没问题，就调 `openi_whoami`。

Claude Desktop / Cursor 等只认 JSON 的客户端，直接写绝对路径即可：

~~~json
{
  "mcpServers": {
    "openi-bridge": {
      "command": "/Users/you/.local/share/openi-bridge/venv/bin/openi-bridge",
      "args": []
    }
  }
}
~~~

服务本身不读当前工作目录，所以这个绝对路径在哪个目录都能用；`OPENI_TOKEN` 环境变量
仍然可以作为兜底，但不再是必需。不想装全局运行时的话，也可以在项目里
`.venv/bin/pip install -e .` 之后用 `python -m openi_bridge.server` 直接跑（stdio 传输）。

## 怎么用

大多数时候你不需要记住任何工具名，直接用自然语言交代任务，助手会去调对应的工具。
下面这几类说法都能直接生效：

**看家底。** “我还剩多少积分，能开多久？”“今天拿满每日积分了吗？”助手会调
`openi_points` / `openi_points_today` / `openi_estimate_runtime`，最后这个用实时单价和
余额算出每个规格还能开多少机时。

**开卡。** “用一张 910B 开个调试任务，代码仓选 `scratch-repo`，跑完给我 Jupyter 地址。”
助手先 `openi_list_creation_options(job_type="DEBUG", compute_source="NPU")` 拿实时
`spec_id` 和镜像，再 `openi_start_task` 建任务，`openi_get_task` 跟状态，
`openi_task_debug_url` 取入口。

**起一个在线服务。** “把这个仓里的 `serve.py` 起成在线推理，给我一个外网地址。”
助手用 `openi_start_task(job_type="ONLINEINFERENCE", compute_source="GPU", boot_file="serve.py")`
建任务（在线推理只支持 GPU，当前账号同时只能开一个），等它 RUNNING 后用
`openi_task_service_url` 拿到 `https://<hex>.mlunotebook.openi.org.cn` 这样的地址。
容器里的服务要监听 8000 端口——平台默认把外部流量转到这个端口，且不会往容器注入
`PORT` / `SERVICE_PORT` 之类的环境变量，服务自己写死或按自己的约定读端口。这个地址在
服务预热好之前会返回 `code 99`（“The service is loading”），隔一会儿再取即可。在线
推理任务不会自动停，会一直计费，用完记得 `openi_stop_task` / `openi_delete_task`。

**临时跑一段代码。** “在云脑上跑 `print(torch.cuda.is_available())` 看看有没有卡”，或者
“开张 GCU 跑一下 `efsmi`”。助手调 `openi_run_on_card`，建任务、连 Jupyter 内核执行、
把 stdout 带回来、顺手清理，全过程你不用管任务 ID。

**传数据集和 ckpt。** “把 `./data` 传成一个数据集叫 `my-corpus`”，“把这个 20G 的
`ckpt/` 传到模型库 `llama-ft` 下”。助手先用 `openi_create_dataset` /
`openi_create_model` 建库（库名固定的 `当前账号/name` 形式），再用
`openi_upload_dataset_files` / `openi_upload_model_files` 传文件——小文件走直传、
超过 64MB 自动按 64MB 分片续传。传完返回里的 UUID 直接就是 `openi_start_task` 的
`dataset_uuid_str` / `pretrain_model_id_str`。“这个数据集还剩多少空间”用
`openi_storage_usage` 看。传文件时最好给绝对路径——`paths` 相对的是 MCP 服务进程的
工作目录，不一定是当前对话所在的目录。传之前可以 `dry_run=True` 先看一眼清单和总量。

**用平台上现成的东西。** “OpenI 上有没有能直接用的 llama 权重？”“给我找个图像分类的
数据集，拉到本地看看”。助手用 `openi_search_models` / `openi_search_datasets` 搜平台
托管的模型与数据集（关键词、类别、任务、按下载量/收藏数排序、只看官方推荐都可以），
返回里每项都带 `subject`（`owner/name`）、UUID 和体积；`openi_list_subject_files`
可以继续看某个库里的文件清单，`openi_pull_model` / `openi_pull_dataset` 把它拉到本地
（支持只拉单个文件、`dry_run` 先看体积、按大小跳过已完成文件、`max_bytes` 上限）。

如果不打算下载到本地，直接把库挂到云脑任务上更省事：`openi_start_task` 的
`dataset_uuid_str` / `pretrain_model_id_str` 既能填 UUID，也能直接填 `owner/name`
（逗号分隔多个），工具会自动换成 UUID——也就是说搜到一个公开 ckpt 后可以直接
`openi_start_task(..., pretrain_model_id_str="FoundationModel/llama_model_7B")`，
不用先把十几 GB 的权重拖下来。

**改代码再开卡。** “把这个目录传成一个新仓 `my-train`，把 `train.py` 里
`epochs=10` 改成 `epochs=1`，然后拿一张 V100 跑一遍。”助手用
`openi_create_repository` 建仓，`openi_push_repo_files` 把本地目录按原结构提交上去
（每个文件一个提交，不需要本地 git），`openi_read_repo_file` / `openi_write_repo_file`
读改写代码，再 `openi_start_task` 指定这个仓和分支。`openi_list_repo_files` 传
`path` 可以翻子目录。建仓和删仓在平台上都要跑几十秒，工具已经放宽了超时；超时不代表
失败，回查 `openi_list_my_repositories` 确认即可。想一次看全仓结构用 `openi_repo_tree`，
确认提交落地用 `openi_list_repo_commits`，整份备份到本地用 `openi_download_repo_archive`，
报问题用 `openi_create_repo_issue` / `openi_comment_repo_issue`。用完
`openi_delete_repository` 删仓。

**收拾现场。** “把我的调试任务都停掉/删掉。”助手用 `openi_list_tasks` 找到任务，再
`openi_stop_task` / `openi_delete_task`。

**收产出、查为什么排队。** “任务跑完了，把 output 里的结果拿下来”“这个任务怎么一直排队？”
助手用 `openi_task_outputs` / `openi_task_output_files` 列任务产出的文件，
`openi_task_download_output` 把文件取下来（小文件直接读进返回，大文件给 `save_to` 流式
落盘），`openi_task_download_log` 取整份日志；任务排队、被抢占或失败时用
`openi_task_events` 看调度时间线（分配节点→拉镜像→建容器→启动→结束），
`openi_task_nodes` 看跑在哪些节点上，`openi_task_loss` 读训练 loss 曲线。注意任务输出在
平台侧是异步索引的，刚 SUCCEEDED 时可能还列不到文件，隔一两分钟重试。

想手动调工具或写脚本，命令行入口和 MCP 工具是一一对应的：

~~~bash
python -m openi_bridge.exec_on_card --code "print(1+1)"
python -m openi_bridge.exec_on_card --source GCU --code "!efsmi"
python -m openi_bridge.autopoint --status
python -m openi_bridge.install_mcp --apply
~~~

完整的 67 个工具见下面的[工具全表](#工具全表)。从零创建任务的顺序是固定的：`openi_whoami`
→ `openi_list_creation_options` → 选代码仓和分支 → `openi_start_task` →
`openi_get_task` / `openi_task_debug_url` → 用完 `openi_stop_task`，必要时
`openi_delete_task`。想省掉“自己找仓、自己配 spec、自己挂数据集”这几步，就先用
`openi_list_task_templates` 挑一个别人跑通的模板，再 `openi_start_task_from_template`。

## 配置令牌

1. 打开 https://openi.pcl.ac.cn/user/settings/applications ，新建一个访问令牌
   （勾选需要的作用域）。
2. 推荐写进 `~/.openi/token.json`（`openi login` 也是写到这里）：

~~~json
{ "endpoint": "https://openi.pcl.ac.cn", "token": "你的令牌" }
~~~

令牌解析顺序是：工具参数 → `~/.openi/token.json` → 环境变量 `OPENI_TOKEN`。也就是说
环境变量只是兜底，日常用文件最省事——MCP 配置里不用放令牌，换令牌也不用改客户端配置。
临时用一下可以走环境变量：

~~~bash
export OPENI_TOKEN=你的令牌
# 可选，默认就是 https://openi.pcl.ac.cn
export OPENI_ENDPOINT=https://openi.pcl.ac.cn
~~~

## 算力与镜像

普通用户能用的集群是 `C2Net`（智算网络），`cluster` 必须填 `C2Net`，`cluster_type`
查询参数同理。`openi_list_creation_options(job_type=..., compute_source=...)` 会返回当前
账号真实可开的**规格**（`spec_id`，即某个卡型 × 卡数的规格编号）、可用镜像、默认任务名和
积分余额。`compute_source` 取值有 `GPU` / `NPU` / `GCU` / `MLU` / `DCU` /
`ILUVATAR-GPGPU` / `METAX-GPGPU` / `BIREN-GPU` / `CPU`。

镜像分两种，实测情况不一样。

- **NVIDIA GPU**：平台给 GPU 登记的两个 `image_id` 在 C2Net 上都会以
  `image not available` 失败，`/ai_task/creation/image_by_spec` 也返回空。真正能被集群
  拉起的是镜像地址，从 `openi_list_public_images` 拿（平台推荐镜像的 `place` 字段），例如：

~~~text
dockerhub.pcl.ac.cn:5000/user-images/openi:SemanticSegmentEnv
swr.cn-south-1.myhuaweicloud.com/openi/cuda111_python37_pytorch191:v1
~~~

- **NPU / GCU / MLU / DCU / ILUVATAR-GPGPU / METAX-GPGPU / BIREN-GPU**：
  `image_id` 是好的，直接用 `openi_list_creation_options` 里的 `image_id`/`image_name`。

不传镜像时工具会自动处理：GPU 走 `OPENI_DEFAULT_IMAGE_URL`（默认
`swr.cn-south-1.myhuaweicloud.com/openi/cuda111_python37_pytorch191:v1`，平台识别为
`cuda111_python37_pytorch191_public`）；其余算力优先用实测跑通的镜像
（`src/openi_bridge/resources.py` 里的 `VERIFIED_IMAGES`，按 `spec_id` 记录），命中就填这个
`image_id`，没命中再退回平台列出的第一个。返回里的 `_image_source` 会说明走了哪条路径
（`fallback_image_url` / `verified_image` / `platform_default_image` / `explicit`）。

除 NVIDIA GPU 外，创建任务**必须带 `branch_name`**（代码分支），否则集群会以
`Fail to load code` 失败（`CREATED_FAILED`）。工具在你不传时默认用代码仓的默认分支，
`_branch_source` 会说明来源。

上面的结论来自逐个算力厂商开 DEBUG 任务、在容器里跑该厂商自带 SMI 的真实输出（2026-10）。
每行都标了当时实测用的任务号，可以在平台上直接查，原始输出在
[`docs/verified-outputs/`](docs/verified-outputs/)：

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

除了纯 CPU，这些算力都能真正开到卡。`METAX-GPGPU`（沐曦 C500）平台上有可用规格
（`spec_id=333`），但 2026-10-13 23:00 到 2026-10-14 09:12 之间反复创建了 9 次 DEBUG
任务，7 次卡在 `centerPending`（示例任务 000000、000000）、2 次 `CREATED_FAILED`，一次都
没有调度到卡，任务详情里的 `ai_center_code` 还是 `openi-mlu`，看起来没有真正的沐曦资源
池，所以上表里没有它。`CPU` 是唯一在 `openi_list_creation_options` 里拿不到规格的
`compute_source`。

## 计费与约束

- 消耗积分 = 单价 × 卡时 × 节点数，每 6 分钟结算一次，不足 6 分钟不计费；只有 `RUNNING`
  才计费，排队和 `PREPARING` 不计费。
- 调试（DEBUG）任务通常有单次时长上限（例如 4 小时），到期自动结束。
- 同一作业类型同时只能有一个运行/排队中的任务，新建前先停掉旧任务。
- 任务名以小写字母或数字开头，只能包含小写字母、数字、`_` 和 `-`，不能以 `_` 结尾，
  最长 36 字符。
- 积分余额和明细见 https://openi.pcl.ac.cn/reward/point 。

想知道余额还能开多久，直接调 `openi_estimate_runtime`：它用实时的 `unit_price` 和当前
余额，按“每 6 分钟向下取整”的口径算出每个 `spec_id` 还能开多少小时/天。例如余额
4891.8 分时，单卡 V100（spec 210，6 分/卡时）约 815 小时，单卡 910A（spec 328，
2 分/卡时）约 2445 小时，910C（spec 392，8 分/卡时）约 611 小时。

## 积分

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

## 在卡上跑代码

平台没有给 SSH，也不接受批处理脚本，DEBUG 任务唯一稳定的交互入口是它自带的 JupyterLab。
`openi_run_on_card` 把这条路补上：取 `debug_url` 里带 token 的地址，走 Jupyter 的 REST
建内核，再连它的内核 WebSocket 发 `execute_request`，把 stdout、返回值、异常收回来，
最后无论成功失败都在 `finally` 里停掉并删除任务。

~~~bash
python -m openi_bridge.exec_on_card --code "print(1+1)"
python -m openi_bridge.exec_on_card --source GCU --code "!efsmi"
python -m openi_bridge.exec_on_card --spec-id 184 --script probe.py --exec-timeout 600
python -m openi_bridge.exec_on_card --code "import torch; print(torch.cuda.is_available())" --keep
~~~

代码里可以用 `!cmd` 跑 shell，所以查厂商的 smi（`efsmi` / `npu-smi info` / `cnmon` /
`ixsmi` / `mx-smi` / `brsmi` / `hy-smi`）或者 `nvidia-smi` 都是一行的事。默认挑最便宜的
规格（1 分/卡时的 GCU ENFLAME-T20），用 `--source` / `--spec-id` 换别的卡；代码仓默认取
账号下第一个非空仓库，用 `--repo` 或 `OPENI_EXEC_REPO` 指定。

计费的只有任务 RUNNING 的那段时间，而不足 6 分钟不计费，所以跑个 print、查个 smi 这类
几秒的探测净消耗 0 分——实测在 GCU spec 229 上建任务、跑 `efsmi`、删除，余额前后都是
4891.8 分。真正的成本是排队时间：实测这次从 create 到 RUNNING 等了 216 秒（排队不计费）。
要长时间跑就用 `--keep` 保留任务，报告里会给出 Jupyter 地址，用完自己停掉删除，否则会
持续计费；`autopoint --reap` 也会顺带回收遗留的 `openixec-*` 任务。

## 工具全表

共 67 个工具，都是 `openi_` 前缀。

| 工具 | 作用 |
| --- | --- |
| openi_whoami | 查看令牌对应的账号，确认令牌可用 |
| openi_list_resources | 列出集群、卡型、积分单价与计费规则（离线目录） |
| openi_list_compute_resources | 查平台实时在线的算力中心（城市）与卡型（含离线目录没有的 CPU/5090/A800 等） |
| openi_notifications | 查看账号通知（评论、@、issue），可只看未读并带未读总数 |
| openi_list_creation_options | 列出当前账号实时可开的规格（含 spec_id）与镜像、余额 |
| openi_estimate_runtime | 按当前积分余额估算每个规格还能开多少机时（小时/天，已按 6 分钟向下取整） |
| openi_points | 查看积分余额、单日获取上限与平台公布的完整获取规则 |
| openi_points_history | 查看积分流水（获取/消耗明细，10 条一页，按天汇总） |
| openi_points_today | 看今天（北京时间）已经拿到多少分、算力那 10 分还在不在、还剩哪些每日项 |
| openi_claim_daily_points | 需要时自动跑一遍"开任务→RUNNING→停止→删除"，把当天 10 分算力积分拿到，净消耗 0 分 |
| openi_autopoint_schedule | 生成 launchd plist 与 crontab 配置，把上面这条挂成每日定时任务 |
| openi_run_on_card | 在云脑的一张卡上直接跑一段代码：建 DEBUG 任务 → 连它的 Jupyter 内核执行 → 收 stdout → 清理 |
| openi_list_images_for_spec | 查询某个 spec 可用的镜像（C2Net 上通常为空） |
| openi_list_public_images | 列出可真正拉起的公共镜像地址（平台推荐 + 自己收藏） |
| openi_list_my_repositories | 列出自己的代码仓（创建任务要指定代码仓） |
| openi_search_repositories | 搜索公开代码仓 |
| openi_create_repository | 新建代码仓（owner 固定为当前账号），建完可直接推代码去跑任务 |
| openi_delete_repository | 删除代码仓（连同代码与提交历史，不可恢复） |
| openi_push_repo_files | 把本地文件/目录提交进代码仓，每文件一个提交，不需要本地 git |
| openi_read_repo_file | 读取仓内单个文件（可落盘），用于确认启动入口或超参 |
| openi_write_repo_file | 新建或覆盖仓内一个文本文件，产生一个提交 |
| openi_list_repo_files | 列出代码仓某个目录的文件（含类型/大小）与全部分支，用于确定启动文件 |
| openi_repo_tree | 一次拿到整仓文件树（路径/类型/大小），比逐层翻目录快 |
| openi_list_repo_commits | 列出提交历史（时间倒序），核对代码是不是真的推上去了 |
| openi_download_repo_archive | 把整仓打包成 zip 下载到本地；max_bytes 超限即中止，不留残包 |
| openi_create_repo_issue | 在代码仓里开 issue，返回 issue 号 |
| openi_list_repo_issues | 列出代码仓的 issue（open / closed / all） |
| openi_comment_repo_issue | 在指定 issue 下发表评论 |
| openi_get_task | 按任务 ID 查询详情与状态 |
| openi_list_tasks | 列出当前账号的全部任务（跨所有代码仓） |
| openi_list_repo_tasks | 列出某个代码仓下的所有任务 |
| openi_task_logs | 读取任务日志 |
| openi_task_resource_usage | 查看任务实时算力占用 |
| openi_task_debug_url | 获取调试任务的 Jupyter / SSH 入口 |
| openi_task_visualize_url | 获取训练任务的 TensorBoard 入口 |
| openi_task_service_url | 获取在线推理（ONLINEINFERENCE）任务的对外服务地址 |
| openi_task_events | 看任务的调度事件时间线（分配节点→拉镜像→建容器→启动→结束），定位为什么排队/失败 |
| openi_task_nodes | 列出任务实际跑在哪些节点上（多机任务多条） |
| openi_task_outputs | 列出任务输出目录的文件（一次一层，子目录用 parent_dir 下翻） |
| openi_task_output_files | 一次列出任务输出的全部文件（递归，name 即相对路径，可直接下载） |
| openi_task_download_output | 下载任务输出的某个文件（流式落盘，或直接读进返回） |
| openi_task_download_log | 下载任务的完整日志（整段纯文本，非分页；读进返回时默认截到 256KB） |
| openi_task_loss | 读取训练任务的 loss 曲线（epoch / loss 序列） |
| openi_list_task_templates | 浏览任务模板广场：别人配好的一键开任务模板（含代码仓、分支、启动文件、卡型卡数、镜像、数据集/模型挂载） |
| openi_get_task_template | 按模板 ID 查一个模板的完整配置 |
| openi_collect_task_template | 收藏 / 取消收藏任务模板 |
| openi_start_task_from_template | 照着模板开任务：仓/分支/启动文件/挂载自动带上，spec 由卡型+卡数实时配对 |
| openi_start_task | 新建并启动计算任务（调试/训练/推理） |
| openi_start_task_raw | 用自定义 JSON 请求体创建任务（兜底/调试用） |
| openi_stop_task | 停止任务、释放算力、停止计费 |
| openi_restart_task | 重启已停止的任务 |
| openi_delete_task | 删除任务（需先停止） |
| openi_sdk_code | 生成容器内访问数据集/模型的示例代码 |
| openi_create_dataset | 新建空数据集（返回 UUID，可作 dataset_uuid_str） |
| openi_create_model | 新建空模型库（返回模型 ID，可作 pretrain_model_id_str） |
| openi_data_subject_info | 查数据集/模型详情，拿 UUID 与是否有写权限 |
| openi_upload_dataset_files | 把本地文件/目录上传到数据集（>64MB 自动分片续传，可 dry_run 先看清单） |
| openi_upload_model_files | 把本地文件/目录（ckpt 等）上传到模型库（同样支持 dry_run） |
| openi_storage_usage | 查看账号或单个主体的存储配额（上限/已用/剩余） |
| openi_delete_dataset | 删除数据集（全名或 UUID，不可恢复） |
| openi_delete_model | 删除模型库（全名或 UUID，不可恢复） |
| openi_search_datasets | 搜索平台托管的数据集（关键词/类别/任务/许可证/排序/只看推荐，返回体积与 UUID） |
| openi_search_models | 搜索平台托管的模型与 ckpt（关键词/标签/排序/推荐，返回体积、下载量、是否外链） |
| openi_list_subject_files | 列出任意数据集/模型库里的文件与子目录及大小（>1000 行用 marker 翻页） |
| openi_delete_subject_file | 删除数据集/模型库里的一个文件或整个子目录（删完重新列清单核对，本来没有则报 existed=false） |
| openi_pull_dataset | 从 OpenI 拉取数据集文件到本地（单文件或整库，支持 dry_run 与 max_bytes） |
| openi_pull_model | 从 OpenI 拉取模型/ckpt 文件到本地（单文件或整库，支持断点跳过） |

## 实现说明与已知限制

本工具基于平台前端实际调用的 REST 接口（`/api/v1/...`）实现，创建任务的请求体取自平台
源码 `entity.CreateReq`。已用真实令牌验证过的接口与行为：

- 账号与代码仓：`/user`、`/user/repos`（`POST` 建仓）、`DELETE /repos/{owner}/{repo}`
  删仓、`/repos/{owner}/{repo}`、`/branches`、`/right`（写权限）、`/contents` 目录列表，
  以及文件读写 `GET/POST/PUT /repos/{owner}/{repo}/contents/{path}`（POST 新建、PUT 覆盖
  需带旧 blob sha）。
- 代码仓的 git 面：`/git/trees/{ref}?recursive=1` 一条请求返回整棵树（响应里的
  `truncated` 表示被平台截断）、`/commits` 提交历史、`/archive/{ref}.zip` 整仓打包、
  `/issues`（GET 列表 / POST 新建）、`/issues/{n}/comments`（POST 评论）。
- 资源发现：`/ai_task/creation/required` 返回当前账号在指定 `job_type` + `compute_source`
  + `cluster_type` 下真实可用的规格列表（含数字 `spec_id`、卡型、卡数、CPU/内存、单价）、
  可用镜像、默认任务名、积分余额，以及 `can_create_more` / `not_stop_task_count` 等配额信息。
- 镜像目录：`/images/recommend`（平台推荐）与 `/images/star`（本账号收藏），其中的 `place`
  才是集群实际拉取的镜像地址。
- 算力资源目录：`/resources/ai_center/card_info/active` 是在线卡型（比
  `/resources/acc_card/list` 多出 CPU 的 INTEL XEON、GPGPU 的 N100/N260、GPU 的
  5090/A800），`/resources/ai_center/active` 给每个算力中心的城市与卡型。
- 任务模板：`GET /ai_task_template/list/{scope}`（`scope` 取 `public` / `all` /
  `created` / `collected`，返回体是 `{Page, PageSize, Total, Templates}`）、
  `GET /ai_task_template?id=<uuid>`（单条详情，响应是 `{code,msg,data}` 信封），
  收藏走 `PUT` / `DELETE /ai_task_template/collect?id=`。模板自带 JobType、Cluster、
  ComputeSource、AccCardType、AccCardsNum、RepoOwnerName/RepoName、BranchName、
  BootFile 与数据集/模型清单，但没有 spec_id，`openi_start_task_from_template` 是按
  `(compute_source, acc_card_type, acc_cards_num)` 去 `/ai_task/creation/required`
  的实时清单里配对的（实测 NPU MNIST 模板配到 spec 328，挂上 MnistDataset_mindspore
  开 DEBUG 任务能到 RUNNING，代码仓可以直接用模板自带的公开示例仓）。
- 任务读写：`/ai_task`、`/ai_task/brief`、`/ai_task/log`、`/ai_task/resource_usage`、
  `/ai_task/debug_url`、`/ai_task/visualize_url`、`/ai_task/self_endpoint_url`（在线推理
  的对外地址）、`/ai_task/my_list`（账号级任务列表）、`/{owner}/{repo}/ai_task`（按仓列表）。
- 任务观测：`/ai_task/log/download`（整份日志纯文本，不是 JSON）、
  `/ai_task/operation_profile`（调度事件时间线，每条含 reason/timestamp/message，时间戳
  是 UTC）、`/ai_task/node_info`（节点清单）、`/ai_task/loss`（训练 loss 曲线）、
  `/ai_task/output`（输出目录，一次一层，`parent_dir` 下翻）、`/ai_task/output/all`
  （一次返回递归全量清单，`FileName` 就是相对路径）、`/ai_task/output/download`
  （下载单个输出文件，平台先答 307 再跳到 OBS 签名直链，`file_name` 给纯文件名或
  相对路径都能命中）。
- 任务变更：`POST /{owner}/{repo}/ai_task/create`、`/ai_task/stop`、`/ai_task/restart`、
  `/ai_task/del`、`/ai_task/batch_del`。
- 数据集与模型：`GET /dataset?dataset_name=`、`GET /aimodel?aimodel_name=`（拿 UUID 与
  `can_edit_file`）、`GET /dataset/files`、`GET /aimodel/files`、`GET /storage/summary`；
  创建走 `POST /dataset/create` 与 `POST /aimodel/create`（JSON 体，`owner_id` 必须是整数），
  删除走 `POST /{dataset|aimodel}/batch_delete?dataset_ids=...`。
- 广场检索：`GET /dataset/list/{scope}` 与 `GET /aimodel/list/{scope}`，`scope` 取
  `public` / `owned` / `collected` / `collaborated`（对应平台数据集/模型页的四个 tab）。
  查询参数 `page`、`page_size`、`q`、`order_by`、`tags`、`tasks`、`license`、`recommend`；
  返回体是 `{datasets|aimodels, total, page, page_size}`。公开数据集体量约 1 万、
  模型约 2.9 万，`recommend=only` 时是首页那 40 个数据集 / 37 个模型。
- 文件清单：`GET /dataset/files` 与 `/aimodel/files` 单页最多 1000 行，用 `marker`
  翻页（响应里的 `has_next` / `marker`），每行是 `FileName`、`IsDir`、`Size`、`ModTime`。
- 从平台下载：`GET /dataset/file` 与 `/aimodel/file` 对托管在 OBS 上的文件返回
  **301**，`Location` 就是带签名的直链，直接对 OBS 发 GET 即可，无需再带令牌
  （工具内部走的就是这条路径）。整库打包另有
  `GET /{dataset|aimodel}/download`，返回一个含 `openi_resource.version` 的 zip。
- 文件上传：小文件走 `GET /upload/direct/get_upload_url` → `PUT` → `POST
  /upload/direct/complete_upload`；大于 64MB 走 `GET /upload/get_chunks` →
  `GET /upload/new_multipart` → 每片 `GET /upload/get_multipart_url` + `PUT` →
  `POST /upload/complete_multipart`。实测 70MB 文件分片上传成功，重复上传会命中
  `get_chunks` 的 `uploaded` 直接跳过（断点续传）。

需要注意：

- 上传到对象存储的 `PUT` 用的是预签名地址，签名把请求头也算进去了：带 `Authorization`
  或 `Content-Type` 都会返回 `SignatureDoesNotMatch`，所以这条请求只发文件体本身。
- 数据集/模型名（`name`）只能是字母/数字/连字符/下划线/点，字母数字开头结尾；`alias`
  （中文展示名）只接受中文/字母/数字/连字符/下划线/点，且不含空格。
- 分片的 md5 不是整文件 md5，而是「每个 64MB 分片取前 1MB」串起来算的值，与官方 openi
  SDK 一致；`get_file_md5` 用错会导致续传判定失败。
- 上传前会先查 `/storage/summary` 比对剩余配额；单账号默认 100GB。`/storage/summary`
  带 `subject_id` 时实测仍返回账号级汇总，所以按主体维度的剩余额度只能作参考。
- 文件本身落在华为 OBS（`obs.cn-south-222.ai.pcl.cn`）上，是持久化存储：上传成功后
  不随任务或会话消失，删除走 `openi_delete_subject_file`（单个文件或子目录）或
  `openi_delete_dataset` / `openi_delete_model`（整个库，`batch_delete`）。
- 删除单个文件走 `DELETE /{dataset|aimodel}/file?file_name=`，平台对**不存在的路径也
  返回 `{"code":0,"msg":"ok"}`**（删一个不存在的 `ghost.txt` 也回 ok，实测），只看响应
  就会把「根本没删」当成删掉了。工具因此先确认路径存在，删完再重新列一遍父目录，清单里
  确实没有了才报 `removed=true`，本来就没有则报 `existed=false`。
- 代码仓写入走 Gitea 风格的 `contents` 接口：新建用 `POST`、覆盖用 `PUT` 且必须带旧
  blob 的 sha（工具会先查一次 sha 再决定用哪个动词），因此**每个文件一个提交**。平台的
  多文件提交入口（`POST /repos/{owner}/{repo}/contents` 批量、`POST /git/blobs`）在这套
  部署上都是 404，所以没有批量提交这条路径。
- `openi_repo_tree` 走 `/git/trees/{ref}?recursive=1`，一条请求拿全，大仓能返回几万条，
  所以默认截到 20000 条并用 `truncated` 标出来。`openi_list_repo_commits` 走 `/commits`，
  它的 `path` 过滤参数在这套部署上是空操作（实测传任何 path 都返回全量历史），所以工具
  没有暴露这个参数；要看某个文件的内容直接 `openi_read_repo_file`。
- `openi_download_repo_archive` 走 `/archive/{ref}.zip`，边下边写盘，先落在
  `<目标>.part` 下完再改名，中途失败或超过 `max_bytes` 都会把临时文件删掉，不会留下
  半截 zip。
- 任务输出（`/ai_task/output*`）在平台侧是**异步索引**的：任务刚进 `SUCCEEDED` 时
  `output.status` 是 1、`file_list` 是空的，通常要等一两分钟才变成 `status=0` 并列出
  文件（实测 000000 / 000000 都在约两分钟后才刷出来）。所以 `openi_task_outputs` /
  `openi_task_output_files` 带 `ready` 标记：刚跑完看不到文件不代表任务没有输出，隔一会儿
  重试即可。输出目录里的子目录以 `IsDir=true` 标出，下载子目录里的文件用相对路径
  （如 `sub/mid.txt`）或者“文件名 + `parent_dir`”两种写法都行。
- 单文件走 base64（整个文件进内存），因此 `openi_push_repo_files` 有单文件上限
  `MAX_REPO_FILE_BYTES`（50MB）；更大的文件请改用 local git 推送。
- 没有做成工具的 `DELETE /repos/{owner}/{repo}/contents/{path}`：这条路由在平台上会一直
  挂到代理的 60 秒超时（504/502）且不删文件，包一层只会给出假的成功，所以故意不提供。
  要换内容就覆盖写，要清空就删掉整个仓。
- 建仓（`POST /user/repos`）与删仓（`DELETE /repos/{owner}/{repo}`）在平台侧都要跑几十秒
  （实测建仓 30 秒以上、删仓约 7 秒），工具默认给 180 秒超时；超时不代表失败，请回查
  `openi_list_my_repositories` 确认，不要重复建仓。
- 检索与下载用的是 REST 面（`/api/v1/...`），和积分那批 web 接口不同：这里带
  `Authorization: Bearer`，而不需要 Basic。注意 `httpx.Client(base_url=...)` 必须写成
  `"{endpoint}/api/v1"`，只填 endpoint 会让 `/aimodel/file` 之类返回 404。
- 广场列表的 `page_size` 上限是 100：传 200 会被平台静默压成默认的 12 条，所以工具
  在 1-100 之外直接报错。反过来，`tags` / `tasks` 传字符串会被 `",".join()` 按字符拆开
  （`tags=computer_vision` 变成 `c,o,m,...`），平台照样返回 200 但结果是 0 条——工具
  内部已把字符串按逗号切分，列表和逗号串两种写法都吃。
- 数据集接口支持 `tasks` 和 `license` 过滤（`license=MIT` 实测从 1 万条筛到 1099 条），
  但模型接口实测忽略这两个参数（传进去仍返回全量），所以 `openi_search_models` 没有
  它们，找模型请用 `q` 或 `tags`。
- 文件清单一页最多 1000 行，靠响应里的 `has_next` / `marker` 翻页；
  `openi_list_subject_files` 把 `marker` 透传出来了（20 万张图的目录要翻 200 页），
  递归下载走的是内部循环，不受影响。
- 重复上传同名文件是覆盖，不会出现两份（实测二次上传后目录里仍然只有一个 `a.txt`）；
  但 ≤64MB 的直传不做跳过判断，重传会把字节重发一遍，只有分片路径会命中
  `get_chunks` 的 `uploaded` 跳过已完成分片。
- 上传目录只跳过 `.DS_Store` / `Thumbs.db`，`.git` 之类的隐藏目录会一起传上去；
  传代码目录时注意先清理。
- 下载不做重试：单个文件失败会记在返回的 `failed` 里，重跑会跳过已完成文件，所以
  半途断掉直接再跑一次即可。下载的总时长不设上限（怕大 ckpt 被掐断），但每次读有
  120 秒超时，连接被静默挂住时不会无限等。
- 拉取时如果平台返回的文件名里含 `..`，写到目标目录之外的路径会被跳过并计入 `failed`。
- 目录遍历默认上限 20000 个文件（环境变量 `OPENI_WALK_FILE_LIMIT` 可调），
  `openi_pull_dataset` / `openi_pull_model` 另有 `max_files` 参数；文件更多的数据集
  必须显式放宽才拉得全，否则会直接报错而不是悄悄少拉。
- 外链模型（返回里 `external: true`，`external_name` / `external_url` 非空）在平台上
  没有实际文件，`openi_list_subject_files` 会是空、`openi_pull_model` 也拉不到东西；
  真正托管在自己对象存储里的模型 `external` 为 false。
- 大主体（例如 `Open_Dataset/imagenet`，143GB）会触发全量遍历，因此
  `openi_pull_dataset` / `openi_pull_model` 在 `max_bytes` 有值且未指定 `file` 时，
  先读主体的声明体积做一次拦截；真要全量拉请显式调大或省略 `max_bytes`。
- `openi_start_task` 的 `dataset_uuid_str` / `pretrain_model_id_str` 支持逗号分隔的
  `owner/name`，工具会先查回 UUID 再创建任务；挂载靠平台把数据迁到集群
  （任务会经过 `dataMigrating`），所以公开 ckpt 可以直接挂、不必先下载。
- 数据集/模型库的整库 zip 接口（`/{dataset|aimodel}/download`）没有单独做成工具：逐文件
  拉取已经能拿到同样的内容，还多出 `dry_run` 预览、按大小跳过已下载文件和断点续传的能力，
  而 zip 一次性把整包放进内存/磁盘、中途失败要重来。需要 zip 时可直接对该接口发请求。
  代码仓的打包下载是另一条路由，做成了 `openi_download_repo_archive`。
- 普通用户可用的集群是 `C2Net`（智算网络）。`cluster` 必须填 `C2Net`，`cluster_type`
  查询参数同理；`openi` 之类的值会返回参数错误或空规格。
- 任务模板按 ID 取详情只有 `GET /ai_task_template?id=<uuid>` 一条路：
  `/ai_task_template/detail`、`/ai_task_template/<uuid>` 都是 404，列表接口的 `id`
  参数也是摆设（传了照旧返回首页，实测过）；列表 `page_size` 上限同样是 100。公开
  模板约 1600 个、含私有约 2700 个。`order_by` 只认 `newest` / `usecount` /
  `collections`，写别的平台不报错、直接按默认顺序返回，所以工具显式拦住；`tags` 是按
  模板自己的标签精确匹配（大小写敏感，取值多为中文），不是广场数据集那种类别枚举。
- 模板的 `ImageUrl` 不一定是镜像地址：NPU 的 MNIST 模板给的是内部记录 UUID
  （`318c8cac-...`），当镜像地址发下去必然拉不起来。`openi_start_task_from_template`
  默认忽略模板镜像、改用实测跑通的镜像，要指定就用 `image_url`；模板确实固定了特殊
  镜像（如在线推理的 `openclaw-feishu-qwen3.5`）时才把 `use_template_image` 置 True。
  模板里的 `Parameters`（表单默认值）也没有自动映射到 `params`，需要时自己传。
- `openi_start_task_from_template` 不填 `repo` 时用模板自带的仓，公开模板多是示例仓
  （如 `OpenIOSSG/OpenI_Cloudbrain_Example`），实测可以直接在上面开任务，不必先建仓。
- 创建任务的必填字段是 `job_type`、`display_job_name`、`spec_id`（整数）、
  `compute_source`、`cluster`；`spec_id` 必须来自 `openi_list_creation_options`，否则会
  返回 Specification not available。
- 创建是异步的：`create` 立即返回任务 ID（状态 `PREPARING`），随后集群异步拉起。如果集群
  侧排队或拉起失败，任务会进入失败态，且不计费；用 `openi_get_task` 观察状态。
- DEBUG 任务不需要 `boot_file`；训练类任务需要以 `.py` 结尾的 `boot_file`。
- 在线推理（`job_type=ONLINEINFERENCE`）**只支持 GPU**：同样的 job_type 配
  `compute_source=NPU` 时 `/ai_task/creation/required` 返回的规格清单是空的，开不了。
  GPU 下实测给的是 A100 与 V100 两档（spec 237 / 210，单价 9 / 6 分/卡时），`images`
  字段为 `None`，所以不指定镜像时会走 `image_url` 默认镜像（实测能拉起）。配额上
  `limit_count` 通常是 1，即同一时刻只能有一个在线推理任务（`can_create_more` 反映
  当前是否还能开）。
- 在线推理任务把外部流量转到容器内的 **8000 端口**（任务详情的 `port` 字段就是这个值，
  `end_point` 是平台自动生成的一段 hex）。平台**不会**向容器注入 `PORT` / `SERVICE_PORT`
  之类的端口环境变量（实测 `env_PORT` 为 null），服务必须自己监听 8000。任务 RUNNING
  后 `GET /ai_task/self_endpoint_url?id=` 返回真实地址（形如
  `https://<hex>.mlunotebook.openi.org.cn`）。这个地址在任务还是 WAITING 时就已返回，
  但那会儿服务没起来、连不上；预热期间该接口也可能返回 `code 99`
  （"The service is loading"），隔一会儿重试即可。实测在这条地址上 GET 与 POST 都能
  收到容器里服务的响应。
- 在线推理的自定义访问路径与端口在请求体里的字段名是 `endpoint` / `port`，但平台对
  这两个值校验很严（只传 `port` 报 2030 "Custom path and port must be filled in"，
  再补 `endpoint` 又变成 9009 系统错误），所以工具没有把它们做成参数，需要时用
  `extra_fields` 自己拼，并接受可能被拒。
- 在线推理任务**不会自动停止**，会一直计费；训练任务里那套 `time_limit` 自动停止在这
  条路径上没有用。实测过程中 `openi_task_logs` 在任务运行期间返回 "The AI task is still
  running"（推理任务不落日志），`openi_task_resource_usage` 返回 6003 "Does not support
  displaying resource usage data yet"，`visualize_url` 返回空串——这几点都是平台行为，
  不是工具的问题。
- 账号级任务列表用的是 `/ai_task/my_list`，它在平台前端真实存在，但字段较少；更完整的信息
  用 `openi_get_task`。
- 镜像：只有 NVIDIA GPU 的登记 `image_id` 在 C2Net 上拉不起来（`image not available`），
  工具对 GPU 会改用 `OPENI_DEFAULT_IMAGE_URL` 并把镜像地址作为 `image_url` 传下去；其余
  算力的 `image_id` 可直接使用。
- `branch_name`：除 NVIDIA GPU 外的算力在创建时都需要它，缺失会以 `Fail to load code`
  （`CREATED_FAILED`）失败；工具默认取代码仓的默认分支。
- 可用性：`GPU` / `NPU` / `GCU` / `MLU` / `DCU` / `ILUVATAR-GPGPU` / `BIREN-GPU` 都已开卡
  验证（见上面的实测表）；`METAX-GPGPU`（沐曦 C500，spec 333）有规格但从未调度到卡——
  连续 9 次 DEBUG 任务里 7 次卡在 `centerPending`、2 次 `CREATED_FAILED`，且任务详情里的
  `ai_center_code` 是 `openi-mlu`，看起来没有真正的沐曦资源池；`CPU` 是唯一拿不到规格的
  `compute_source`。
