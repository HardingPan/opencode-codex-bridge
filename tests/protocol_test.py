#!/usr/bin/env python3
"""协议级回归测试:不起 Codex,直接对 server.py 说 MCP。

覆盖:initialize 握手 → delegate 委派 → status/result 轮询 → 多进程恢复(任务落盘)。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "server.py"


def session():
    return subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    return json.loads(line) if line.strip() else None


def tool_text(response):
    return (response.get("result", {}).get("content") or [{}])[0].get("text", "")


def main():
    ok = True

    proc = session()
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    info = recv(proc)["result"]["serverInfo"]
    print(f"1. initialize ✓ ({info['name']} v{info['version']})")
    send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "opencode_delegate",
                           "arguments": {"action": "run", "task": "请只回复四个字:桥接就绪",
                                         "cwd": "/Users/panding/workspace/SIM_RUN"}}})
    tid = json.loads(tool_text(recv(proc)))["task_id"]
    print(f"2. delegate ✓ task_id={tid}")
    time.sleep(20)

    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "opencode_delegate", "arguments": {"action": "status", "task_id": tid}}})
    st = json.loads(tool_text(recv(proc)))
    print(f"3. status ✓ {st.get('status')} ({st.get('duration_s')}s, {st.get('steps')} 步)")
    ok = ok and st.get("status") == "completed"

    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "opencode_delegate", "arguments": {"action": "result", "task_id": tid}}})
    res = json.loads(tool_text(recv(proc)))
    print(f"4. result ✓ 回复={res.get('text')!r} exit={res.get('exit_code')} 会话={res.get('opencode_session')}")
    ok = ok and res.get("text") == "桥接就绪"
    proc.stdin.close()
    proc.wait(timeout=10)

    # 跨进程恢复:任务记录已落盘,新进程能查到
    proc2 = session()
    send(proc2, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    recv(proc2)
    send(proc2, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "opencode_delegate", "arguments": {"action": "result", "task_id": tid}}})
    res2 = json.loads(tool_text(recv(proc2)))
    recovered = res2.get("text") == "桥接就绪"
    print(f"5. 跨进程恢复 {'✓' if recovered else '✗'} 回复={res2.get('text')!r}")
    ok = ok and recovered
    proc2.stdin.close()
    proc2.wait(timeout=10)

    print("\n" + ("全部通过 ✅" if ok else "存在失败 ❌"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
