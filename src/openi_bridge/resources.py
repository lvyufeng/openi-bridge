"""Static catalog of OpenI compute resources.

Values come from the platform docs:
https://openi.pcl.ac.cn/docs/index.html#/quickstart/resources and
https://openi.pcl.ac.cn/docs/constants.json
"""

from __future__ import annotations

# 消耗积分 = 单价 * 卡时 * 节点数，每 6 分钟结算一次（不满 6 分钟不扣费）。
CARDS: dict[str, dict[str, object]] = {
    "A100": {"vendor": "NVIDIA", "price": 9},
    "V100": {"vendor": "NVIDIA", "price": 6},
    "RTX3090": {"vendor": "NVIDIA", "price": 4},
    "T4": {"vendor": "NVIDIA", "price": 2},
    "BIREN-106M": {"vendor": "壁仞", "price": 1},
    "Ascend910": {"vendor": "华为昇腾", "price": 2, "api": "NPU"},
    "Ascend910B": {"vendor": "华为昇腾", "price": 2, "api": "NPU_910b"},
    "ENFLAME-T20": {"vendor": "燧原", "price": 1, "api": "GCU"},
    "MLU290": {"vendor": "寒武纪", "price": 1, "api": "MLU"},
    "DCU": {"vendor": "海光", "price": 1},
    "Z100L": {"vendor": "海光", "price": 1},
    "K100_AI": {"vendor": "海光", "price": 3},
    "MR-V100": {"vendor": "天数智芯", "price": 1},
    "BI-V100": {"vendor": "天数智芯", "price": 1},
    "N100": {"vendor": "沐曦", "price": 1},
    "N260": {"vendor": "沐曦", "price": 1},
}

CLUSTERS: dict[str, str] = {
    "openi": "启智集群（鹏城云脑，GPU/NPU 等）",
    "c2net": "智算网络集群 Beta",
}

JOB_TYPES: dict[str, str] = {
    "DEBUG": "调试任务（交互式，可开 Jupyter/SSH）",
    "TRAIN": "训练任务（跑完自动停止）",
    "INFERENCE": "批量推理任务",
    "ONLINEINFERENCE": "在线推理 / 模型体验",
    "HPC": "超算任务",
}


def describe_resources() -> str:
    """Render the catalog as JSON for the MCP tool."""
    import json

    payload = {
        "clusters": CLUSTERS,
        "job_types": JOB_TYPES,
        "cards": {
            name: {
                "vendor": meta["vendor"],
                "price_per_card_hour": meta["price"],
            }
            for name, meta in CARDS.items()
        },
        "billing": "消耗积分 = 单价 × 卡时 × 节点数；每 6 分钟结算一次，不足 6 分钟不计费。",
        "points": "积分余额与明细见 https://openi.pcl.ac.cn/reward/point",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

