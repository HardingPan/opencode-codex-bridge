#!/usr/bin/env python3
"""
opencode-codex-bridge — 让 Codex(或任何 MCP 宿主)把任务委派给 OpenCode 执行。

设计要点(与自家的 intercom / heartbeat 同一哲学):
  - 零依赖:纯 Python 标准库实现 MCP stdio 协议(JSON-RPC 2.0,按行分隔)
  - 执行引擎是 `opencode run --format json`(headless),默认 --auto:
    无人在场也能干活;担心越权可在 delegate 时传 auto=false
  - 异步委派:delegate 立即返回 task_id 不阻塞宿主;status / result 轮询;
    cancel 随时终止
  - 支持续接:delegate 时传 session=<上次返回的 opencode 会话 id>,
    即可与同一个 OpenCode 会话多轮迭代

工具(4 个,刻意精简):
  delegate {task, cwd?, model?, agent?, session?, auto?=true}
  status   {task_id}
  result   {task_id}
  cancel   {task_id}

任务记录:~/.config/opencode/codex-bridge/tasks/<id>.json(服务重启后仍可查)
日志:同目录 bridge.log(绝不写 stdout——stdout 是协议通道)

注册到 Codex:
  codex mcp add opencode-delegate -- python3 ~/.config/opencode/codex-bridge/server.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from os.path import expanduser
from pathlib import Path

ROOT = Path(expanduser("~/.config/opencode/codex-bridge"))
TASKS_DIR = ROOT / "tasks"
LOG_FILE = ROOT / "bridge.log"

OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "opencode")

# ---------------------------------------------------------------------------
# 任务执行
# ---------------------------------------------------------------------------

tasks: dict = {}  # task_id → 任务记录(内存;完成后同时落盘)
lock = threading.Lock()


def log(message: str) -> None:
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except Exception:
        pass


def _execute(task: dict) -> None:
    """后台线程:跑 `opencode run`,解析 JSONL 事件,落盘结果。"""
    task_id = task["id"]
    cmd = [OPENCODE_BIN, "run", "--format", "json", "--title", task["title"]]
    if task.get("auto", True):
        cmd.append("--auto")
    if task.get("model"):
        cmd += ["--model", task["model"]]
    if task.get("agent"):
        cmd += ["--agent", task["agent"]]
    if task.get("session"):
        cmd += ["--session", task["session"]]
    cmd.append(task["task"])

    texts: list = []
    session_id = task.get("session") or ""
    steps = 0
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=task.get("cwd") or os.getcwd(),
            stdin=subprocess.DEVNULL,  # 关键:opencode run 在 stdin 非 TTY 时会等输入,必须隔离
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        with lock:
            tasks[task_id]["pid"] = proc.pid
        for line in proc.stdout or []:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            session_id = session_id or str(event.get("sessionID") or "")
            part = event.get("part") or {}
            if part.get("type") == "step-start":
                steps += 1
            elif part.get("type") == "text" and part.get("text"):
                texts.append(str(part["text"]))
        code = proc.wait()
        task["status"] = "completed" if code == 0 else "failed"
        task["exit_code"] = code
    except FileNotFoundError:
        task["status"] = "failed"
        task["error"] = f"找不到 {OPENCODE_BIN} 命令(可用 OPENCODE_BIN 环境变量指定路径)"
    except Exception as exc:  # 委派线程必须吞掉一切异常,状态落盘
        task["status"] = "failed"
        task["error"] = str(exc)
    finally:
        task["finished"] = time.time()
        task["text"] = "\n".join(texts).strip()
        task["opencode_session"] = session_id
        task["steps"] = steps
        _persist(task)
        log(f"task {task_id} {task['status']} exit={task.get('exit_code')}")


def _persist(task: dict) -> None:
    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        keep = {k: v for k, v in task.items() if k != "thread"}
        (TASKS_DIR / f"{task['id']}.json").write_text(
            json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def delegate(args: dict) -> str:
    task_text = str(args.get("task") or "").strip()
    if not task_text:
        return json.dumps({"error": "task 不能为空"}, ensure_ascii=False)

    task = {
        "id": uuid.uuid4().hex[:12],
        "task": task_text,
        "cwd": args.get("cwd") or os.getcwd(),
        "title": f"codex委派-{task_text[:12]}",
        "model": args.get("model"),
        "agent": args.get("agent"),
        "session": args.get("session"),
        "auto": bool(args.get("auto", True)),
        "status": "running",
        "started": time.time(),
    }
    thread = threading.Thread(target=_execute, args=(task,), daemon=True)
    task["thread"] = thread
    with lock:
        tasks[task["id"]] = task
    thread.start()
    log(f"task {task['id']} delegate cwd={task['cwd']} auto={task['auto']}")
    return json.dumps(
        {
            "task_id": task["id"],
            "status": "running",
            "hint": "用 status/result 轮询;传 session 可续接的 opencode 会话稍后在 result 里",
        },
        ensure_ascii=False,
    )


def _find(task_id: str):
    with lock:
        task = tasks.get(task_id)
    if task:
        return task
    path = TASKS_DIR / f"{task_id}.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def status(args: dict) -> str:
    task = _find(str(args.get("task_id") or ""))
    if not task:
        return json.dumps({"error": "任务不存在"}, ensure_ascii=False)
    preview = (task.get("text") or "")[:200]
    return json.dumps(
        {
            "task_id": task["id"],
            "status": task.get("status"),
            "started": task.get("started"),
            "duration_s": round((task.get("finished", time.time()) - task["started"]), 1),
            "steps": task.get("steps"),
            "preview": preview,
        },
        ensure_ascii=False,
    )


def result(args: dict) -> str:
    task = _find(str(args.get("task_id") or ""))
    if not task:
        return json.dumps({"error": "任务不存在"}, ensure_ascii=False)
    if task.get("status") == "running":
        return json.dumps({"task_id": task["id"], "status": "running", "hint": "还没跑完,稍后再取"}, ensure_ascii=False)
    return json.dumps(
        {
            "task_id": task["id"],
            "status": task.get("status"),
            "exit_code": task.get("exit_code"),
            "opencode_session": task.get("opencode_session"),
            "steps": task.get("steps"),
            "error": task.get("error"),
            "text": task.get("text"),
        },
        ensure_ascii=False,
    )


def cancel(args: dict) -> str:
    task_id = str(args.get("task_id") or "")
    with lock:
        task = tasks.get(task_id)
    if not task:
        return json.dumps({"error": "任务不存在(或不在本进程内)"}, ensure_ascii=False)
    pid = task.get("pid")
    if task.get("status") != "running" or not pid:
        return json.dumps({"task_id": task_id, "cancelled": False, "reason": "任务已结束"}, ensure_ascii=False)
    try:
        os.kill(pid, 15)
        task["status"] = "cancelled"
        _persist(task)
        return json.dumps({"task_id": task_id, "cancelled": True}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"task_id": task_id, "cancelled": False, "error": str(exc)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# MCP stdio 协议(零依赖手写)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "delegate",
        "description": (
            "把一个自包含的任务委派给 OpenCode 在指定目录中 headless 执行,立即返回 task_id 不阻塞,"
            "用 status/result 轮询取结果。可选:model(provider/model#variant,如 bailian/deepseek-v4.1-flash#low)、"
            "agent、session(传上次 result 里的 opencode_session 可与同一会话多轮迭代)、"
            "auto(默认 true 自动批准权限;false 则无人在场可能卡在审批)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "要执行的任务描述,需自包含"},
                "cwd": {"type": "string", "description": "执行目录(绝对路径),默认当前目录"},
                "model": {"type": "string", "description": "覆盖模型,如 bailian/kimi-k3#low"},
                "agent": {"type": "string", "description": "覆盖 agent,如 build/plan"},
                "session": {"type": "string", "description": "续接的 OpenCode 会话 id"},
                "auto": {"type": "boolean", "description": "自动批准权限,默认 true"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "status",
        "description": "查询委派任务的状态与输出预览",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "result",
        "description": "获取已完成任务的完整结果(回复文本、exit code、可续接的 opencode 会话 id)",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "cancel",
        "description": "终止运行中的委派任务",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]

HANDLERS = {"delegate": delegate, "status": status, "result": result, "cancel": cancel}


def handle(request: dict):
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": (request.get("params") or {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "opencode-codex-bridge", "version": "0.1.0"},
            },
        }

    if method == "notifications/initialized":
        return None  # 通知,无需响应

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        if name not in HANDLERS:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": f"未知工具 {name}"}], "isError": True},
            }
        try:
            text = HANDLERS[name](params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": f"工具执行出错:{exc}"}], "isError": True},
            }

    if request_id is not None:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"未知方法 {method}"}}
    return None


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    log("bridge 启动,等待宿主指令")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        response = handle(request)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
