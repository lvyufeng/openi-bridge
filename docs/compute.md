# 算力与镜像

平台有哪些卡、怎么选镜像、哪些规格能开。价格与可用性随平台变化，
**实时数据**请用 `openi_list_creation_options` / `openi_list_compute_resources` 查，
下面的表是离线目录，只作参考。


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

`VERIFIED_IMAGES` 里的条目要**同时**满足两个条件才会被采用：带 `verified: "yes"` 标记，
且 `image_id` 是 32 位十六进制（平台 image_id 的形态）。只写进表里、但没核实过的值不会
覆盖平台默认镜像——这张表一旦混入错误的值，代价是任务起不来还照常扣积分，所以宁可退回
平台自己列的镜像。910A（spec 328）与 BIREN106M（spec 347）先前记的两个值没有可追溯的
实测证据，已按此规则停用。

除 NVIDIA GPU 外，创建任务**必须带 `branch_name`**（代码分支），否则集群会以
`Fail to load code` 失败（`CREATED_FAILED`）。工具在你不传时默认用代码仓的默认分支，
`_branch_source` 会说明来源。

上面的结论来自逐个算力厂商开 DEBUG 任务、在容器里跑该厂商自带 SMI 的真实输出（2026-10）。
每行都标了当时实测用的任务号，可以在平台上直接查，原始输出在
[`docs/verified-outputs/`](verified-outputs/)：

| 算力类型 | 实测卡型 | 验证命令 | 实测任务号 |
| --- | --- | --- | --- |
| GPU | NVIDIA Tesla V100S-PCIE-32GB（32768 MiB，驱动 570.124.06，CUDA 12.8） | `nvidia-smi` | TASK-05 |
| NPU | 华为昇腾 910B2（HBM 65536 MB，npu-smi 25.2.1，aarch64） | `npu-smi info` | TASK-09 |
| NPU | 华为昇腾 910ProA / 910A（HBM 32768 MB，npu-smi 24.1.0，aarch64） | `npu-smi info` | TASK-08 |
| NPU | 华为昇腾 910C（双芯，每芯 HBM 65536 MB，npu-smi 26.0.rc3） | `npu-smi info` | TASK-10 |
| GCU | 燧原 Enflame T20（32 G 显存，EFSMI V1.21.0，驱动 2.5.20231219） | `efsmi` | TASK-04 |
| MLU | 寒武纪 MLU290-M5（32768 MiB，CNMON v4.20.16） | `cnmon` | TASK-07 |
| DCU | 海光 DCU / K100_AI（HCU，PwrCap 400W） | `hy-smi`、`rocm-smi` | TASK-03 |
| ILUVATAR-GPGPU | 天数智芯 Iluvatar MR-V100（32768 MiB，IX-ML 驱动 4.4.0） | `ixsmi` | TASK-06 |
| BIREN-GPU | 壁仞 Biren106M（32512 MiB，400W，BR-SMI 1.10.1 / SUPA 1.10） | `brsmi` | TASK-01 |

除了纯 CPU，这些算力都能真正开到卡。`METAX-GPGPU`（沐曦 C500）平台上有可用规格
（`spec_id=333`），但 2026-10-13 23:00 到 2026-10-14 09:12 之间反复创建了 9 次 DEBUG
任务，7 次卡在 `centerPending`（示例任务 TASK-19、TASK-20）、2 次 `CREATED_FAILED`，一次都
没有调度到卡，任务详情里的 `ai_center_code` 还是 `openi-mlu`，看起来没有真正的沐曦资源
池，所以上表里没有它。`CPU` 是唯一在 `openi_list_creation_options` 里拿不到规格的
`compute_source`。
