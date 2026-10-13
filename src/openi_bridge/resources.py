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
    "RTX3090": {"vendor": "NVIDIA", "price": 4, "source": "GPU"},
    "T4": {"vendor": "NVIDIA", "price": 2, "source": "GPU"},
    "Ascend910": {"vendor": "华为昇腾", "price": 2, "source": "NPU"},
    "Ascend910B": {"vendor": "华为昇腾", "price": 2, "source": "NPU"},
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
    "METAX-C500": {"vendor": "沐曦", "price": 2, "source": "METAX-GPGPU"},
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
    "坑": (
        "/ai_task/creation/required 返回的两个 image_id 在 C2Net 上会以 "
        "'image not available' 失败，image_by_spec 也返回空。"
    ),
    "可用做法": "用 image_url 指定镜像地址（place），例如平台推荐镜像里的 place。",
    "默认镜像": (
        "不传镜像时工具会使用 OPENI_DEFAULT_IMAGE_URL，默认是 swr.cn-south-1.myhuaweicloud.com/"
        "openi/cuda111_python37_pytorch191:v1（可验证拉起）。"
    ),
}


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
        "image_notes": IMAGE_NOTES,
        "live_specs": "实时可开规格（含 spec_id）请调用 openi_list_creation_options。",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

