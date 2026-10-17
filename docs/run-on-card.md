# 在卡上跑代码


平台没有给 SSH，也不接受批处理脚本，DEBUG 任务唯一稳定的交互入口是它自带的 JupyterLab。
`openi_run_on_card` 把这条路补上：取 `debug_url` 里带 token 的地址，走 Jupyter 的 REST
建内核，再连它的内核 WebSocket 发 `execute_request`，把 stdout、返回值、异常收回来，
最后无论成功失败都在 `finally` 里停掉并删除任务。

清理是否成功会反映到返回的 `ok` 上：如果任务没能确认停掉（比如删除被平台拒绝），
`ok=false`、`action=cleanup_failed`，并带上 `cleanup` 明细——留一个还在跑的任务等于
持续扣积分，这不能算一次成功的调用。连接 notebook 的 TLS 默认校验证书；
平台证书链握手失败时再用 `OPENI_TLS_INSECURE=1` 显式放行。

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
