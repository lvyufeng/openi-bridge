# 实测输出（厂商 SMI）

每个文件是一次 C2Net DEBUG 任务里、在该任务的 Jupyter 中跑厂商自带 SMI 的原始输出片段。
创建时间 2026-10-14，任务都已停止并删除，输出为当时抓取。

| 文件 | 算力 | spec_id | 任务号 | 关键内容 |
| --- | --- | --- | --- | --- |
| `gpu-v100-task-000000.txt` | NVIDIA GPU | 210 | 000000 | `nvidia-smi`：Tesla V100S-PCIE-32GB，32768 MiB，驱动 570.124.06 / CUDA 12.8 |
| `npu-910a-task-000000.txt` | 昇腾 NPU | 328 | 000000 | `npu-smi info`：910ProA，HBM 32768 MB，npu-smi 24.1.0，aarch64 |
| `npu-910b-task-000000.txt` | 昇腾 NPU | 389 | 000000 | `npu-smi info`：910B2，HBM 65536 MB，npu-smi 25.2.1，aarch64 |
| `npu-910c-task-000000.txt` | 昇腾 NPU | 392 | 000000 | `npu-smi info`：Ascend910 双芯，每芯 HBM 65536 MB，npu-smi 26.0.rc3 |
| `gcu-t20-task-000000.txt` | 燧原 GCU | 229 | 000000 | `efsmi`：T20，32 G 显存，EFSMI V1.21.0 / 驱动 2.5.20231219 |
| `mlu290-task-000000.txt` | 寒武纪 MLU | 184 | 000000 | `cnmon`：MLU290-M5，32768 MiB，CNMON v4.20.16 |
| `dcu-k100-task-000000.txt` | 海光 DCU | 342 | 000000 | `hy-smi` / `rocm-smi`：HCU，PwrCap 400W |
| `iluvatar-mrv100-task-000000.txt` | 天数智芯 GPGPU | 388 | 000000 | `ixsmi`：Iluvatar MR-V100，32768 MiB，IX-ML 驱动 4.4.0 |
| `biren106m-task-000000.txt` | 壁仞 GPU | 347 | 000000 | `brsmi`：Biren106M，32512 MiB，BR-SMI 1.10.1 / SUPA 1.10 |

`METAX-GPGPU`（沐曦 C500，spec 333）没有输出：2026-10-14 的 7 次 DEBUG 任务全部停在
`centerPending` 排队，从未调度到卡，因此没有可抓取的 SMI 输出。
