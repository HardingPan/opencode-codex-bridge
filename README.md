# opencode-codex-bridge

让 **Codex 当指挥**、**OpenCode 当执行**的零依赖 MCP 桥:把任务委派给
OpenCode 用 headless 方式跑(用你配置好的模型,如 bailian),异步取结果。

```
Codex(指挥) ──MCP(stdio)──▶ 本桥 ──`opencode run --auto`──▶ OpenCode 会话(执行)
```

## 特点(与自家 intercom / heartbeat 同一哲学)

- **零依赖**:纯 Python 标准库手写 MCP stdio 协议,无 pip/npm 包
- **异步委派**:`delegate` 立即返回 `task_id` 不阻塞宿主;`status` / `result` 轮询;`cancel` 终止
- **多轮迭代**:`delegate` 传 `session=<上次 result 里的 opencode_session>`,可与同一个
  OpenCode 会话继续对话
- **任务落盘**:记录在 `~/.config/opencode/codex-bridge/tasks/`,桥重启后仍可查结果
- **模型/目录/agent 可选**:每次委派可指定 `model`(如 `bailian/deepseek-v4.1-flash#low`)、
  `cwd`、`agent`(build/plan)
- 委派出的会话以 `codex委派-…` 命名,在你的 OpenCode 会话列表里清晰可辨

## 工具(1 个,精简到底,Codex 侧常驻约 150 token)

| 工具 | action | 说明 |
|---|---|---|
| `opencode_delegate` | `run` | 委派任务(`task` 必填;可选 `cwd`/`model`/`agent`/`session`/`auto`),返回 task_id |
| | `status` | 查状态与输出预览 |
| | `result` | 完整结果(回复文本、exit code、可续接会话 id) |
| | `cancel` | 终止运行中的任务 |

`auto=true`(默认)自动批准 OpenCode 侧权限——无人在场也能干活;传 `false`
则可能卡在审批上,慎用。

## 安装

```bash
mkdir -p ~/.config/opencode/codex-bridge
cp server.py ~/.config/opencode/codex-bridge/
codex mcp add opencode-delegate -- python3 ~/.config/opencode/codex-bridge/server.py
```

## 在 Codex 里怎么用

直接对 Codex 说,例如:

> 用 opencode-delegate 把「给 src/utils 下的所有导出函数补 JSDoc 注释」委派给
> OpenCode 在当前仓库执行,等结果并总结改动。

> 委派一个探索任务给 OpenCode(用 deepseek flash 模型):分析这个项目的目录
> 结构和依赖,返回要点列表。

Codex 会调用 `opencode_delegate`(action=run)→ 拿到 `task_id` → 轮询 `status` →
`result` 取回 OpenCode 的回复。

## 测试

```bash
python3 tests/protocol_test.py   # 协议级回归:握手/委派/轮询/跨进程恢复
```

## 排障

- 日志:`~/.config/opencode/codex-bridge/bridge.log`(stdout 是协议通道,日志绝不写 stdout)
- 任务记录:`~/.config/opencode/codex-bridge/tasks/<id>.json`
- opencode 不在 PATH 时,用 `OPENCODE_BIN` 环境变量指定路径(注册时加 `--env OPENCODE_BIN=...`)

## 已知边界

- 桥进程退出时,未完成的任务会被中断(记录停留在 running);重启后可通过任务文件查历史
- 长任务靠 Codex 侧轮询,桥本身不做超时(有 `cancel` 兜底)
- 实现备注:子进程必须 `stdin=DEVNULL`——`opencode run` 在 stdin 非 TTY 时会等待
  输入直到 EOF,否则委派会挂到宿主断开才执行(踩过的坑,见 `_execute`)

## 许可

MIT
