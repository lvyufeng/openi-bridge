# 实测输出

这个目录是"这些结论是打过的，不是推测的"的凭据。两类内容：各厂商卡的 SMI 原始输出，
以及各条工具链路的端到端实测记录。

抓取时间 2026-10-14 ~ 2026-10-17，用的都是真实令牌，相关任务与临时仓库事后已停止并
删除，无残留。

关于脱敏，这份记录只保留可复核的技术事实（卡型、驱动/内核版本、字节数、平台行为），
不含指向具体账号的信息：任务号统一替换为 `TASK-NN` 占位符，账号名与自建仓名一并抹去，
`DEBUG_URL` 里的 `token=` 一概不保留（抓取时就抹掉），notebook 服务主机名与内网
IP:port 也换成了占位形式。所以下面表格里的 `TASK-NN` 是本文档内部的编号，**不能**
拿去平台上查——它们的作用是让你能对上"哪一行结论出自同一次实测"。

需要说明的是，这次脱敏针对的是"指向某个账号的信息"（任务号、账号名、自建仓名、
notebook 主机），**不是**凭据清理：仓库里从来没有出现过令牌、密码或 access token
（`OPENI_TOKEN` 只在环境变量里，`DEBUG_URL` 的 `token=` 抓取时就已抹掉）。另外，
改写前的历史 commit 仍可按 SHA 从 GitHub 的 HTTP API 读到，git 协议则拿不到；
留着它们没有额外风险，因为里面同样没有任何凭据。

`code-repo-e2e.txt` 不是 SMI 输出，而是代码仓读写链路的实测记录：建仓 →
推文件 → 覆盖写 → 用这个仓跑 TRAIN 任务，任务日志证明跑的是被覆盖写过的代码。

`task-template-e2e.txt` 也不是 SMI 输出，是任务模板链路的实测记录：取模板 →
按卡型卡数配对实时 spec → 开任务 → RUNNING → 停止 → 删除。

`online-inference-e2e.txt` 是在线推理链路的实测记录：建 ONLINEINFERENCE 任务 →
取对外服务地址 → 对地址发 GET / POST 拿到容器里服务的响应 → 停止 → 删除。这份记录了
平台默认端口 8000、地址在 WAITING 阶段就已返回、以及自定义路径/端口会被平台拒的细节。

`task-observability-e2e.txt` 是任务观测链路的实测记录：跑一个短 TRAIN 任务 →
看调度事件时间线（operation_profile）、节点清单、输出目录（含子目录）、下载输出文件、
下载整份日志、读 loss。这份记录了输出的异步索引行为（刚 SUCCEEDED 时 file_list 为空、
约两分钟后才刷出）和下载接口 307 跳 OBS 签名直链的细节。

`task-export-issue-notification-e2e.txt` 是产物转存与 issue/通知链路的实测记录：任务输出
导出成数据集与模型库、issue 的关闭/重开/改标题正文、通知全部标记已读。

`task-lifecycle-e2e.txt` 是任务生命周期（建 → 跑 → 停 → 删）的真机验证记录，用于
确认"回读确认"那套逻辑在真实平台上确实成立。这份同时记录了两个平台侧现象：等待期间
一次 502 会打断轮询（已修），以及卡在 `PREPARING` 的任务既停不掉也删不掉、但会占住
DEBUG 并发额度。

`task-output-archive-repo-account-surface-e2e.txt` 是整包输出下载 + 仓只读治理面 + 账号
发现面的实测记录。这份在 2026-10-17 复测过一次，已删除的任务号在平台上与不存在的号一样
回 403，因此原稿里的部分计数无法追溯，文中已分别标注"已验证"与"待复核"。

| 文件 | 算力 | spec_id | 任务号 | 关键内容 |
| --- | --- | --- | --- | --- |
| `gpu-v100.txt` | NVIDIA GPU | 210 | TASK-05 | `nvidia-smi`：Tesla V100S-PCIE-32GB，32768 MiB，驱动 570.124.06 / CUDA 12.8 |
| `npu-910a.txt` | 昇腾 NPU | 328 | TASK-08 | `npu-smi info`：910ProA，HBM 32768 MB，npu-smi 24.1.0，aarch64 |
| `npu-910b.txt` | 昇腾 NPU | 389 | TASK-09 | `npu-smi info`：910B2，HBM 65536 MB，npu-smi 25.2.1，aarch64 |
| `npu-910c.txt` | 昇腾 NPU | 392 | TASK-10 | `npu-smi info`：Ascend910 双芯，每芯 HBM 65536 MB，npu-smi 26.0.rc3 |
| `gcu-t20.txt` | 燧原 GCU | 229 | TASK-04 | `efsmi`：T20，32 G 显存，EFSMI V1.21.0 / 驱动 2.5.20231219 |
| `mlu290.txt` | 寒武纪 MLU | 184 | TASK-07 | `cnmon`：MLU290-M5，32768 MiB，CNMON v4.20.16 |
| `dcu-k100.txt` | 海光 DCU | 342 | TASK-03 | `hy-smi` / `rocm-smi`：HCU，PwrCap 400W |
| `iluvatar-mrv100.txt` | 天数智芯 GPGPU | 388 | TASK-06 | `ixsmi`：Iluvatar MR-V100，32768 MiB，IX-ML 驱动 4.4.0 |
| `biren106m.txt` | 壁仞 GPU | 347 | TASK-01 | `brsmi`：Biren106M，32512 MiB，BR-SMI 1.10.1 / SUPA 1.10 |
| `code-repo-e2e.txt` | NVIDIA GPU | 210 | TASK-02 | 代码仓链路：建仓/推文件/覆盖写/用该仓跑 TRAIN，日志出现被写入的 `print` |
| `task-template-e2e.txt` | 昇腾 NPU | 328 | TASK-17 | 任务模板链路：模板 → 实时 spec 配对 → 开任务 → RUNNING → 停止并删除 |
| `online-inference-e2e.txt` | NVIDIA GPU | 237 | TASK-11 | 在线推理链路：建 ONLINEINFERENCE → 取服务地址 → GET/POST 拿到响应 → 停止并删除 |
| `task-observability-e2e.txt` | 燧原 GCU | 229 | TASK-13 | 任务观测链路：调度事件/节点/输出目录/下载文件/整份日志/loss；输出异步索引约两分钟 |

`METAX-GPGPU`（沐曦 C500，spec 333）没有输出。2026-10-14 连续 9 次 DEBUG 任务都没
调度到卡，而且失败方式不一致：前 7 次（含 TASK-19、TASK-20）一直停在 `centerPending`
排队，第 8 次（TASK-21）在 `PREPARING` 约 2 分钟后变成 `CREATED_FAILED`，第 9 次
（TASK-22）同样在 `PREPARING` 停留约 2 分 14 秒后 `CREATED_FAILED`、日志为空。
任务详情里的 `ai_center_code` 是 `openi-mlu`，即这个 `compute_source` 被路由到的
其实是寒武纪那套中心，看不出存在真正的沐曦 C500 资源池，所以至今没有可抓取的 SMI 输出。
