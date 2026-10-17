# 客户端配置

[`python -m openi_bridge.install_mcp --apply`](../README.md#安装) 会一次配好四个客户端。
这份文档给两种情况用：想手工配某一个，或者想搞清安装器到底写了什么。

**四个客户端的配置形状互不相同**，其中两个会拒绝最直觉的写法——照着别家的样子抄是最容易
踩的坑。

所有客户端都只放**启动命令**，不放令牌：运行时从 `~/.openi/token.json`（或环境变量
`OPENI_TOKEN`）读，所以配置里没有明文密钥，换令牌也不用改配置。

## 令牌

令牌解析顺序是：**工具参数 → `~/.openi/token.json` → 环境变量 `OPENI_TOKEN`**。
日常用文件最省事；临时用一下可以走环境变量：

```bash
export OPENI_TOKEN=你的令牌
# 可选，默认就是 https://openi.pcl.ac.cn
export OPENI_ENDPOINT=https://openi.pcl.ac.cn
```

`~/.openi/token.json` 的内容：

```json
{ "endpoint": "https://openi.pcl.ac.cn", "token": "你的令牌" }
```

注意 MCP 客户端进程通常**不继承你 shell 里 export 的变量**，所以长期用请写文件。

---

## Claude Code

写在 `~/.claude.json` 的 `mcpServers`；用官方 CLI 写入最省事：

```bash
claude mcp add --scope user --transport stdio openi-bridge \
  /Users/you/.local/share/openi-bridge/venv/bin/openi-bridge
```

验证：`claude mcp list` 应显示 `openi-bridge ✔ Connected`（会话里也可以 `/mcp` 查看）。

## Codex

追加到 `$CODEX_HOME/config.toml`，没设 `CODEX_HOME` 就是 `~/.codex/config.toml`。
**可能存在多份 home**（例如桌面端与 TUI 各一份），每份都要加：

```toml
[mcp_servers.openi-bridge]
command = "/Users/you/.local/share/openi-bridge/venv/bin/openi-bridge"
args = []
```

验证：`codex mcp list`。

## dsh

dsh 把 profile 定义成"插件包 + 用户覆盖"的叠加，MCP 服务器是其中一类插件条目。用户层是
`$DSH_HOME/profiles/<profile>/cordis.patch.yml`，**每个 profile 各一份**：

```yaml
- insert:
    - id: mcp-openi-bridge
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: stdio
        serverName: openi-bridge
        command: /Users/you/.local/share/openi-bridge/venv/bin/openi-bridge
        args: []
```

两个坑：

1. **文件是顶层 YAML 数组**，默认模板就是一行 `[]`。要**替换**它，不要往后接——追加会
   让文件变成两个 YAML 文档，dsh 直接解析失败（`document separator is expected`）。
2. 它是**人手维护**的 YAML，里面有注释和自己的插件。装机器只做行级插入、不重新序列化，
   手改时也一样——round-trip 一次注释就全没了。

验证：

```bash
dsh --profile dsh-tui --dump-config | grep -A 6 mcp-openi-bridge
```

## Cursor

一条命令，注意 JSON 是**扁平**的并且**必须带 `name`**：

```bash
cursor --add-mcp '{"name":"openi-bridge","command":"/Users/you/.local/share/openi-bridge/venv/bin/openi-bridge","args":[]}'
```

传 Claude 那种 `{"mcpServers": {"openi-bridge": {...}}}` 嵌套形式会被拒：
`Missing name property in {...}`。

## Claude Desktop 及其他 JSON 客户端

这类客户端吃**嵌套**形式（和 Cursor 相反）：

```json
{
  "mcpServers": {
    "openi-bridge": {
      "command": "/Users/you/.local/share/openi-bridge/venv/bin/openi-bridge",
      "args": []
    }
  }
}
```

---

## 形状速查

| 客户端 | 外层结构 | 特别注意 |
| --- | --- | --- |
| Claude Code | `mcpServers` 映射 | 用 CLI 写，别手改 |
| Codex | TOML section | 可能有多份 home |
| dsh | **顶层数组** | 默认 `[]` 要替换；别重新序列化 |
| Cursor | **扁平对象** | 必须带 `name`；不能嵌套 |
| Claude Desktop | `mcpServers` 映射 | 与 Cursor 相反 |

## 写入是安全的

安装器改任何配置文件之前都会**带时间戳备份**（例如 `config.toml.20261017…bak`），
写入走"临时文件 + `os.replace`"，所以写到一半失败（磁盘满、进程被杀）不会留下被截断的
配置——客户端下次启动也不会因为配置损坏而崩。

dsh 的补丁文件例外：它是人手维护的 YAML，安装器只做**行级插入**，不重新序列化。

## 不装全局运行时

在项目里 `pip install -e .` 之后直接用模块形式启动，MCP 客户端里填：

```json
{ "command": "/path/to/venv/bin/python", "args": ["-m", "openi_bridge.server"] }
```

服务走 stdio 传输，且不读当前工作目录，所以这个绝对路径在哪个目录都能用。