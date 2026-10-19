# openi-bridge

[![tests](https://github.com/lvyufeng/openi-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/lvyufeng/openi-bridge/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

[OpenI 启智社区](https://openi.pcl.ac.cn)（鹏城云脑普惠算力）的 [MCP](https://modelcontextprotocol.io)
服务器。装上之后，在 **Claude Code / Codex / dsh / Cursor** 里直接说人话就能开卡、
跑训练、取产物，不用记住任何工具名。

> `www.openi.org.cn` 只是社区门户，真正的算力平台是
> [openi.pcl.ac.cn](https://openi.pcl.ac.cn)，本工具对接的是后者。

## 能做什么

- **查**：账号、积分余额、实时可开的规格与镜像、平台在线的算力中心
- **开**：调试 / 训练 / 批量推理 / 在线推理任务；也能在卡上直接跑一段代码并取回 stdout
- **看**：任务状态、日志、调度时间线、loss 曲线、Jupyter / TensorBoard / 服务地址
- **收**：产物列清单、下载、整包打 zip、转存成数据集或模型库长期留存
- **代码仓**：建仓、读写文件、提交历史、issue、tag/release/label/collaborator
- **数据**：建数据集/模型库、上传（>64MB 自动分片续传）、检索、拉回本地
- **积分**：每日算力积分自动领取（零净消耗），可挂定时任务

共 **86 个工具**，完整列表见 [`docs/tools.md`](docs/tools.md)。

## 安装

需要 Python ≥ 3.10。

**1. 拿一个令牌。** 打开 https://openi.pcl.ac.cn/user/settings/applications 新建访问令牌，
写进 `~/.openi/token.json`（注意权限，见下）：

```json
{ "endpoint": "https://openi.pcl.ac.cn", "token": "你的令牌" }
```

**2. 装一份不依赖当前目录的运行时。**

```bash
python3 -m venv ~/.local/share/openi-bridge/venv
~/.local/share/openi-bridge/venv/bin/pip install -e /path/to/openi-bridge
```

**3. 注册到你的客户端。** 自带的安装器会把启动命令算成绝对路径，一次写进四个客户端
（装了几个就配几个），改前自动备份：

```bash
# 先干跑，看会写什么
~/.local/share/openi-bridge/venv/bin/python -m openi_bridge.install_mcp
# 确认无误再真写
~/.local/share/openi-bridge/venv/bin/python -m openi_bridge.install_mcp --apply
```

| 客户端 | 配置位置 |
| --- | --- |
| Claude Code | `~/.claude.json`（走 `claude mcp add -s user`） |
| Codex | `$CODEX_HOME/config.toml`（默认 `~/.codex/config.toml`；多份 home 都写） |
| dsh | `$DSH_HOME/profiles/<profile>/cordis.patch.yml`（每个 profile 一份） |
| Cursor | 用户 profile（走 `cursor --add-mcp`） |

**4. 重启客户端并验证。** `claude mcp list` 应显示 `openi-bridge ✔ Connected`，
然后随便找个目录问一句"OpenI 上现在有哪些卡能开、我还剩多少积分"。

配置里**只有启动命令、没有令牌**，运行时从 `~/.openi/token.json` 读，所以换令牌不用改配置。
要手工配某一个客户端（四个客户端的配置形状互不相同，其中两个会拒绝最直觉的写法），
见 [`docs/install.md`](docs/install.md)。

## 用法

大多数时候不用记工具名，直接说需求：

```
用一张 910B 开个调试任务，代码仓选我的 xxx，跑完给我 Jupyter 地址
在云脑上跑 print(torch.cuda.is_available()) 看看有没有卡
把 ./data 传成一个数据集叫 my-corpus
这个任务跑完了，把输出里的 ckpt 转成模型库存起来
我还剩多少积分，能开多久？
```

想直接调工具或写脚本，每个 MCP 工具都有对应的命令行入口：

```bash
python -m openi_bridge.exec_on_card --code "!npu-smi info"
python -m openi_bridge.autopoint --status
```

更多说法与背后的工具、以及从零开一次任务的固定顺序，见
[`docs/usage.md`](docs/usage.md)。

## 工具

按用途分七组，完整参数与说明见 [`docs/tools.md`](docs/tools.md)：

| 分组 | 用途 | 数量 |
| --- | --- | --- |
| 账号与积分 | 确认令牌、看余额、领每日积分、通知 | 9 |
| 算力与规格 | 平台有什么、你能开什么、余额够开多久 | 6 |
| 开卡与跑代码 | 建任务、在卡上跑代码、停/重启/删 | 7 |
| 看任务 | 状态、日志、调度时间线、产物下载与转存 | 19 |
| 代码仓 | 建仓、读写、issue、仓库治理面、找别人的仓 | 27 |
| 数据集与模型库 | 建库、上传、检索、拉回本地 | 14 |
| 任务模板 | 别人配好的一键开任务模板 | 4 |

改状态的工具（停 / 重启 / 删）都会**回读确认**状态真的变了——任务在 RUNNING 时按分钟
计费，把"请求已受理"当成"已经停了"是要花钱的。

## 文档

| 文档 | 内容 |
| --- | --- |
| [`docs/tools.md`](docs/tools.md) | 85 个工具的完整表格与参数 |
| [`docs/usage.md`](docs/usage.md) | 常见说法与典型流程（自然语言 → 工具） |
| [`docs/install.md`](docs/install.md) | 四个客户端的手工配置片段 |
| [`docs/compute.md`](docs/compute.md) | 算力与镜像：有哪些卡、怎么选镜像 |
| [`docs/billing.md`](docs/billing.md) | 计费规则与用量约束 |
| [`docs/points.md`](docs/points.md) | 积分规则与每日自动领取 |
| [`docs/run-on-card.md`](docs/run-on-card.md) | 在卡上跑一段代码的原理与限制 |
| [`docs/limits.md`](docs/limits.md) | 参数上限与防呆 |
| [`docs/platform-notes.md`](docs/platform-notes.md) | 平台接口行为、字段与踩坑记录（改代码前先看） |
| [`docs/verified-outputs/`](docs/verified-outputs/) | 各厂商卡的 SMI 原始输出与端到端实测记录 |

## 开发

```bash
pip install -e ".[dev]"
pytest
```

测试**不依赖网络与令牌**，覆盖的是"错了会悄悄报成功"的那几处逻辑：

- `tests/test_resources.py` —— 镜像可信度闸门（未核实/形态不对的 `image_id` 不得覆盖
  平台默认镜像）、离线算力目录与实测清单的一致性、README 与工具表的一致性、四个客户端的
  配置片段形状
- `tests/test_safety_rails.py` —— 分片续传编号校验（稀疏编号必须报错而不是跳片）、
  任务停止/删除的回读确认、内核执行状态（`abort` 不得算成功）、翻页终止、下载覆盖保护、
  清理失败不得报成功

每条都对应一个真实发生过的静默失败——把修复回退掉就会红。

## 已知限制

- **同时只能有 1 个 running DEBUG 任务**；在线推理同样只能 1 个（平台限制）
- **在线推理只支持 GPU**，容器里的服务必须监听 8000 端口
- **METAX-GPGPU 至今未验证**：连测 9 次都没调度到卡，看不出真有沐曦资源池
- 平台对任务输出是**异步索引**的，刚跑完可能还列不到文件，等一两分钟再试
- 细节与更多约束见 [`docs/limits.md`](docs/limits.md) 与
  [`docs/platform-notes.md`](docs/platform-notes.md)

## License

[MIT](LICENSE)