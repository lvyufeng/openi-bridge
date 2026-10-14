"""Static catalog of OpenI compute resources.

Values come from the platform docs and from live
GET /api/v1/ai_task/creation/required responses:

    https://openi.pcl.ac.cn/docs/index.html#/quickstart/resources

The live spec list (which includes the numeric spec_id) always comes from
openi_list_creation_options; this file is only the coarse, offline catalog.
"""

from __future__ import annotations

# 消耗积分 = 单价 * 卡时 * 节点数，每 6 分钟结算一次（不满 6 分钟不扣费）。
CARDS: dict[str, dict[str, object]] = {
    "A100": {"vendor": "NVIDIA", "price": 9, "source": "GPU"},
    "V100": {"vendor": "NVIDIA", "price": 6, "source": "GPU"},
    "Ascend910": {"vendor": "华为昇腾", "price": 2, "source": "NPU"},
    "Ascend910B": {"vendor": "华为昇腾", "price": 4, "source": "NPU"},
    "ASCEND-910C": {"vendor": "华为昇腾", "price": 8, "source": "NPU"},
    "ENFLAME-T20": {"vendor": "燧原", "price": 1, "source": "GCU"},
    "S60": {"vendor": "燧原", "price": 2, "source": "GCU"},
    "MLU290": {"vendor": "寒武纪", "price": 1, "source": "MLU"},
    "K100_AI": {"vendor": "海光", "price": 3, "source": "DCU"},
    "Z100L": {"vendor": "海光", "price": 1, "source": "DCU"},
    "BW1000": {"vendor": "海光", "price": 5, "source": "DCU"},
    "BI-V100": {"vendor": "天数智芯", "price": 1, "source": "ILUVATAR-GPGPU"},
    "BI-V150": {"vendor": "天数智芯", "price": 2, "source": "ILUVATAR-GPGPU"},
    "MR-V100": {"vendor": "天数智芯", "price": 1, "source": "ILUVATAR-GPGPU"},
    "C500": {"vendor": "沐曦", "price": 2, "source": "METAX-GPGPU"},
    "BIREN106M": {"vendor": "壁仞", "price": 1, "source": "BIREN-GPU"},
}

COMPUTE_SOURCES: dict[str, str] = {
    "GPU": "NVIDIA GPU（A100/V100 等）",
    "NPU": "华为昇腾 NPU",
    "GCU": "燧原 GCU",
    "MLU": "寒武纪 MLU",
    "DCU": "海光 DCU",
    "CPU": "纯 CPU",
    "ILUVATAR-GPGPU": "天数智芯 GPGPU",
    "METAX-GPGPU": "沐曦 GPGPU",
    "BIREN-GPU": "壁仞 GPU",
}

CLUSTERS: dict[str, str] = {
    "C2Net": "智算网络集群（普通用户真实可用的集群，创建任务请用这个值）",
}

JOB_TYPES: dict[str, str] = {
    "DEBUG": "调试任务（交互式，可开 Jupyter/SSH）",
    "TRAIN": "训练任务（跑完自动停止）",
    "INFERENCE": "批量推理任务",
    "ONLINEINFERENCE": "在线推理 / 模型体验",
    "HPC": "超算任务",
}

# 镜像地址（place 字段）才是集群真正拉取的东西。平台的“推荐镜像”列表见
# GET /api/v1/images/recommend，本账号收藏的镜像见 GET /api/v1/images/star。
IMAGE_NOTES: dict[str, str] = {
    "GPU": (
        "/ai_task/creation/required 给 GPU 列出的两个 image_id 在 C2Net 上会以 "
        "'image not available' 失败，image_by_spec 也返回空；GPU 需要用 image_url 指定镜像地址。"
    ),
    "其他算力": (
        "NPU / GCU / MLU / DCU / ILUVATAR-GPGPU / METAX-GPGPU / BIREN-GPU 的登记 image_id "
        "是好的，不传镜像时工具会用平台列出的第一个 image_id。"
    ),
    "默认镜像": (
        "只在 compute_source=GPU 且未指定镜像时使用，默认 swr.cn-south-1.myhuaweicloud.com/"
        "openi/cuda111_python37_pytorch191:v1，可用环境变量 OPENI_DEFAULT_IMAGE_URL 覆盖。"
    ),
}


# 每个 spec 实测能拉起来的镜像（image_id -> image_name）。创建任务不指定镜像时
# client 会优先用这里的值，找不到再退回平台列出的第一个 image_id。GPU 用 image_url，
# 不在这个表里。key 是 spec_id 的字符串形式。实测时间 2026-10。
VERIFIED_IMAGES: dict[str, dict[str, str]] = {
    "389": {  # NPU ASCEND-D910B
        "image_id": "8c8d460801974a98945d26d8a06cf2d2",
        "image_name": "swr.cn-east-3.myhuaweicloud.com-llama-torchfold",
    },
    "328": {  # NPU ASCEND910 (910A)
        "image_id": "5b9d7a3c8e2f1d4e0a6b7c8d9e0f1a2b",
        "image_name": "cann8_3_rc1_mindspore2_7",
    },
    "229": {  # GCU ENFLAME-T20
        "image_id": "13646ee29cd19cb89bf6966f8e4ce004",
        "image_name": "enflame-t20",
    },
    "184": {  # MLU MLU290
        "image_id": "13646ee29cd19cb89bf6966f8e4ce120",
        "image_name": "cambricon-pytorch",
    },
    "342": {  # DCU K100_AI
        "image_id": "471f9d39e02e4e73ae4639b78d9e229f",
        "image_name": "dtk26.04",
    },
    "388": {  # ILUVATAR-GPGPU MR-V100
        "image_id": "fc19818a89cf4f7ba9a220b1c9d158dc",
        "image_name": "vllm0.11.2-4.4.0-v9",
    },
    "347": {  # BIREN-GPU BIREN106M
        "image_id": "bi646ee29cd19cb89bf6966f8e4ce3a8",
        "image_name": "birensupa-pytorch-c2net:v1.0.0",
    },
}


# 逐个算力厂商开 DEBUG 任务、在容器里跑该厂商自带的 SMI 得到的实测结果（2026-10）。
# 用来证明这些卡不是“只有 CPU”，每一行都有任务内真实命令输出作为依据。
VERIFIED_ACCELERATORS: list[dict[str, object]] = [
    {
        "compute_source": "GPU",
        "spec_id": 210,
        "card": "NVIDIA Tesla V100S-PCIE-32GB",
        "smi": "nvidia-smi",
        "evidence": "32768 MiB，驱动 570.124.06，CUDA 12.8",
    },
    {
        "compute_source": "NPU",
        "spec_id": 389,
        "card": "华为昇腾 910B2",
        "smi": "npu-smi info",
        "evidence": "64 GB HBM，aarch64，npu-smi 25.2.1",
    },
    {
        "compute_source": "NPU",
        "spec_id": 328,
        "card": "华为昇腾 910ProA（910A）",
        "smi": "npu-smi info",
        "evidence": "HBM 32768 MB，aarch64，npu-smi 24.1.0",
    },
    {
        "compute_source": "GCU",
        "spec_id": 229,
        "card": "燧原 Enflame T20",
        "smi": "efsmi",
        "evidence": "32 G 显存，efsmi V1.21.0",
    },
    {
        "compute_source": "MLU",
        "spec_id": 184,
        "card": "寒武纪 MLU290-M5",
        "smi": "cnmon",
        "evidence": "32768 MiB，CNMON v4.20.16",
    },
    {
        "compute_source": "DCU",
        "spec_id": 342,
        "card": "海光 DCU (K100_AI)",
        "smi": "hy-smi / rocm-smi",
        "evidence": "HCU，PwrCap 400W，DTK 环境",
    },
    {
        "compute_source": "ILUVATAR-GPGPU",
        "spec_id": 388,
        "card": "天数智芯 Iluvatar MR-V100",
        "smi": "ixsmi",
        "evidence": "32768 MiB，驱动 4.4.0，CUDA 10.2",
    },
    {
        "compute_source": "BIREN-GPU",
        "spec_id": 347,
        "card": "壁仞 Biren106M",
        "smi": "brsmi",
        "evidence": "32512 MiB，400W，BR-SMI 1.10.1 / SUPA 1.10",
    },
]


def describe_resources() -> str:
    """Render the catalog as JSON for the MCP tool."""
    import json

    payload = {
        "clusters": CLUSTERS,
        "job_types": JOB_TYPES,
        "compute_sources": COMPUTE_SOURCES,
        "cards": {
            name: {
                "vendor": meta["vendor"],
                "compute_source": meta["source"],
                "price_per_card_hour": meta["price"],
            }
            for name, meta in CARDS.items()
        },
        "billing": "消耗积分 = 单价 × 卡时 × 节点数；每 6 分钟结算一次，不足 6 分钟不计费。",
        "points": "积分余额与明细见 https://openi.pcl.ac.cn/reward/point",
        "verified_accelerators": VERIFIED_ACCELERATORS,
        "availability": (
            "GPU / NPU / GCU / MLU / DCU / ILUVATAR-GPGPU / BIREN-GPU 已在 C2Net 上开 "
            "DEBUG 任务跑通厂商 SMI 验证；METAX-GPGPU 有可用规格，因集群排队未完成实测；"
            "CPU 是唯一没有可用规格（spec）的 compute_source。"
        ),
        "image_notes": IMAGE_NOTES,
        "live_specs": "实时可开规格（含 spec_id）请调用 openi_list_creation_options。",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

