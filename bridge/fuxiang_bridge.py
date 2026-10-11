# -*- coding: utf-8 -*-
"""
随笔浮想 (Sketch/UGC) 编辑器命令行桥接 — 游戏内常驻模块

由 fuxiang-inject.exe 注入到 dwrg.exe 进程内加载。职责：
  1. 起一个后台线程轮询命令目录 (ipc/cmd/*.json)
  2. 每条命令通过 game3d.frame_delay_exec 派发到**主线程**执行
  3. 结果写入 ipc/out/<id>.json，并维护心跳 ipc/bridge_state.json

设计约束（务必遵守）：
  - 任何异常都不能逃逸，绝不能影响游戏运行
  - 主线程回调必须快速返回，禁止阻塞/长任务
  - 不触碰网络/云端接口（按需求：只做本地建图）
"""

import os
import sys
import json
import time
import threading
import traceback

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
# 2026-09-22：改为按 __file__ 推导。⚠️ 游戏内环境下 __file__ 可能不存在
# （同 fuxiang_edit.py 的防御写法），缺失时回落到安装时的固定路径，
# 绝不因取不到 __file__ 而让桥接起不来。
_this_file = globals().get("__file__")
ROOT = (os.path.dirname(os.path.dirname(os.path.abspath(_this_file)))
        if _this_file
        else (os.environ.get("FUXIANG_TOOL_ROOT") or r"<工具根>"))
BRIDGE_DIR = os.path.join(ROOT, "bridge")
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)
# 收敛 sys.path 里的重复项：2026-10-03 前的 eval 片段每次调用都无条件 insert 一条
# （无守卫），一次 1.5 万件还原会在游戏进程 sys.path 里堆几百条重复桥路径——既是
# 内存驻留字符串也是每次 import 的线性扫描负担。重复项永远冗余，这里一次清干净。
_seen = set()
sys.path[:] = [p for p in sys.path if not (p in _seen or _seen.add(p))]

# eval 危险调用拦截名单的唯一真源（与 fuxiang_edit.op_eval 共用）
import fuxiang_eval_guard as eval_guard  # noqa: E402

IPC = os.path.join(ROOT, "ipc")
CMD_DIR = os.path.join(IPC, "cmd")
OUT_DIR = os.path.join(IPC, "out")
STATE_FILE = os.path.join(IPC, "bridge_state.json")
LOG_FILE = os.path.join(IPC, "bridge.log")

VERSION = "0.1.0"

# 命令轮询间隔。实测（2026-09-19）单次往返固定 ~87ms，其中 0.08 的固定间隔贡献
# 平均 40ms（46%），是最大的单项开销。改为自适应：
#   * 空闲 → POLL_INTERVAL（低频，省 CPU / 少碰文件系统）
#   * 刚处理过命令后的 POLL_ACTIVE_WINDOW 秒内 → POLL_INTERVAL_ACTIVE（高频，低延迟）
# 连续摆放/批量操作期间始终处于活跃态，只有本批第一次要等低频周期。
POLL_INTERVAL = 0.08
POLL_INTERVAL_ACTIVE = 0.005
POLL_ACTIVE_WINDOW = 2.0

# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------
def _mkdirs():
    for d in (IPC, CMD_DIR, OUT_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=repr)
    os.replace(tmp, path)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# 主线程派发
# --------------------------------------------------------------------------
def run_on_main(fn, timeout=20.0):
    """
    通过引擎的 frame_delay_exec 把 fn 派发到主线程执行，等待其结果。
    返回 (ok, value_or_error)。
    """
    import game3d

    box = {"done": False, "value": None, "error": None}

    def wrapper(*_a):
        try:
            box["value"] = fn()
        except BaseException:  # 连 SystemExit/KeyboardInterrupt/MemoryError 都不许逃逸主线程回调
            box["error"] = traceback.format_exc()
        finally:
            box["done"] = True

    try:
        game3d.frame_delay_exec(1, wrapper)
    except Exception:
        return False, "frame_delay_exec failed:\n" + traceback.format_exc()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if box["done"]:
            break
        time.sleep(0.005)

    if not box["done"]:
        return False, "timeout waiting for main-thread execution"
    if box["error"]:
        return False, box["error"]
    return True, box["value"]


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------
def op_ping(_args):
    return {"pong": True, "version": VERSION, "pid": os.getpid()}


def op_info(_args):
    out = {
        "pid": os.getpid(),
        "python": sys.version,
        "threads": threading.active_count(),
        "module_count": len(sys.modules),
    }
    try:
        import game3d
        out["frame_count"] = game3d.get_frame_count()
        out["frame_rate"] = game3d.get_frame_rate()
    except Exception as e:
        out["frame_err"] = str(e)
    # 当前场景 / UGC 状态线索
    try:
        import importlib
        wm = importlib.import_module("worlds.WorldManager")
        out["worldmanager_attrs"] = [a for a in dir(wm) if not a.startswith("_")][:80]
    except Exception as e:
        out["wm_err"] = str(e)
    return out


def op_eval(args):
    """在主线程执行一段 Python 语句，返回 __result__（若有）。

    注意：globals 与 locals 必须是同一个 dict，否则推导式/闭包无法看到
    脚本里的局部变量（Python 的经典作用域陷阱）。

    ⚠️ 禁调名单（引擎某些 API 在编辑器主线程调用会冻结/卡死游戏，持 GIL）：
    必须在此底层也拦截，避免经 fxcall/fxrun/probe 等 CLI 路径绕过上层拦截直接打到游戏。

    2026-09-22 修复：原先本层手写 3 条、edit 层 5 条，**少了 get_submesh_bounding_box
    与 get_submesh_bounding_radius**，直连路径可绕过。现两层共用
    bridge/fuxiang_eval_guard.py 这一唯一真源。
    """
    code = args.get("code", "")
    hit = eval_guard.find_blocked(code)
    if hit:
        pattern, reason = hit
        return {"error": "已拦截危险调用 %r：%s" % (pattern, reason)}

    def fn():
        ns = {"__name__": "fuxiang_eval", "__result__": None}
        exec(compile(code, "<fuxiang-eval>", "exec"), ns, ns)
        return ns.get("__result__")

    ok, val = run_on_main(fn, timeout=float(args.get("timeout") or 20.0))
    if not ok:
        return {"error": val}
    return {"result": val}


def op_modules(args):
    """列出/筛选已加载模块与其中的类方法。"""
    kw = (args.get("keyword") or "").lower()
    names = sorted(sys.modules)
    if kw:
        names = [n for n in names if kw in n.lower()]
    return {"count": len(names), "modules": names[:500]}


def op_introspect(args):
    """对一个已加载模块或类做反射。"""
    target = args.get("target", "")
    parts = target.split(".")
    mod = None
    obj = None
    for i in range(len(parts), 0, -1):
        name = ".".join(parts[:i])
        if name in sys.modules:
            mod = sys.modules[name]
            obj = mod
            for p in parts[i:]:
                obj = getattr(obj, p)
            break
    if obj is None:
        return {"error": "not found: " + target}
    out = {"target": target, "type": type(obj).__name__}
    if isinstance(obj, type):
        ms = [m for m in dir(obj) if not m.startswith("_")]
        out["methods"] = ms
        out["doc"] = (obj.__doc__ or "")[:1000]
    else:
        out["attrs"] = [m for m in dir(obj) if not m.startswith("_")][:300]
    return out


def op_find_spec(args):
    """批量测试模块是否存在于脚本包内（不导入）。"""
    import importlib.util
    names = args.get("names") or []
    res = {}
    for n in names:
        try:
            spec = importlib.util.find_spec(n)
            res[n] = bool(spec)
        except Exception as e:
            res[n] = "%s: %s" % (type(e).__name__, e)
    return {"result": res}


OPS = {
    "ping": op_ping,
    "info": op_info,
    "eval": op_eval,
    "modules": op_modules,
    "introspect": op_introspect,
    "find_spec": op_find_spec,
}


# --------------------------------------------------------------------------
# 调度线程
# --------------------------------------------------------------------------
def _process(cmd):
    cid = cmd.get("id") or ("anon-%d" % time.time())
    op = cmd.get("op")
    args = cmd.get("args") or {}

    if op not in OPS:
        return {"id": cid, "ok": False, "error": "unknown op: %r" % op}

    started = time.time()
    try:
        value = OPS[op](args)
        return {"id": cid, "ok": True, "value": value,
                "elapsed": round(time.time() - started, 3)}
    except BaseException:  # MemoryError/SystemExit 等也不许把异常带上游戏主线程
        return {"id": cid, "ok": False, "error": traceback.format_exc(),
                "elapsed": round(time.time() - started, 3)}


def _poll_loop(stop_evt):
    log("poll loop started tid=%d" % threading.get_ident())
    last_work = 0.0
    while not stop_evt.is_set():
        did_work = False
        try:
            try:
                names = os.listdir(CMD_DIR)
            except Exception:
                names = []
            for n in sorted(names):
                if not n.endswith(".json"):
                    continue
                p = os.path.join(CMD_DIR, n)
                try:
                    cmd = read_json(p)
                except Exception:
                    continue
                try:
                    os.remove(p)
                except Exception:
                    pass
                did_work = True
                result = _process(cmd)
                try:
                    write_json(os.path.join(OUT_DIR, result["id"] + ".json"), result)
                except Exception:
                    log("failed to write result: %s" % traceback.format_exc())
        except Exception:
            log("poll loop error: %s" % traceback.format_exc())
        now = time.time()
        if did_work:
            last_work = now
        # 自适应间隔：刚处理过命令时高频轮询以压低往返延迟，闲置后回到低频。
        time.sleep(POLL_INTERVAL_ACTIVE
                   if now - last_work < POLL_ACTIVE_WINDOW
                   else POLL_INTERVAL)


def _edit_busy_op():
    """读取编辑层当前正在执行的 op（若模块已加载）。用于卡死时归因。"""
    try:
        m = sys.modules.get("fuxiang_edit")
        busy = getattr(m, "_BUSY_OP", None)
        if busy and busy.get("op"):
            return {"op": busy["op"], "since": busy.get("since")}
    except Exception:
        pass
    return None


def _heartbeat_loop(stop_evt, started_at):
    while not stop_evt.is_set():
        try:
            write_json(STATE_FILE, {
                "alive": True,
                "version": VERSION,
                "pid": os.getpid(),
                "started_at": started_at,
                "now": time.time(),
                "main_tid": threading.main_thread().ident,
                "polls": _HEARTBEAT["n"],
                "busy_op": _edit_busy_op(),
            })
            _HEARTBEAT["n"] += 1
        except Exception:
            pass
        time.sleep(1.0)


_HEARTBEAT = {"n": 0}


def install():
    """启动桥接（幂等）。

    崩溃修复：不再在【注入线程】里直接 threading.Thread.start()。
    实测（2026-09-17）：桥接由远程注入线程加载，若在该异线程上创建
    CPython 线程会让 dwrg.exe 整体闪退。因此改为把线程启动**调度到游戏
    主线程**（game3d.frame_delay_exec），线程托管在正常 Python 上下文。
    若无法调度到主线程，才回退到当前线程直接启动。
    """
    _mkdirs()
    mod = sys.modules.get("fuxiang_bridge")
    if mod is not None and getattr(mod, "_STARTED", False):
        return "already-running"
    if mod is not None and getattr(mod, "_SCHEDULED", False):
        return "already-scheduled"

    stop_evt = threading.Event()
    started_at = time.time()

    def _start(*_a):
        _m = sys.modules.get("fuxiang_bridge")
        if _m is not None and getattr(_m, "_STARTED", False):
            return
        globals()["_STARTED"] = True
        globals()["_STOP"] = stop_evt
        t1 = threading.Thread(target=_poll_loop, args=(stop_evt,),
                              name="fuxiang-poll", daemon=True)
        t1.start()
        t2 = threading.Thread(target=_heartbeat_loop, args=(stop_evt, started_at),
                              name="fuxiang-heartbeat", daemon=True)
        t2.start()
        log("bridge installed v%s pid=%d (threads on main)" % (VERSION, os.getpid()))

    globals()["_SCHEDULED"] = True
    try:
        import game3d
        game3d.frame_delay_exec(1, _start)   # 主线程启动，而非注入线程
        return "installed (scheduled on main thread)"
    except Exception:
        # 调度不可用则回退：当前线程直接启动
        globals()["_SCHEDULED"] = False
        try:
            _start()
            return "installed"
        except Exception:
            raise


# 模块被 exec 加载时自动安装
try:
    _INSTALL_RESULT = install()
except Exception:
    try:
        log("install failed: %s" % traceback.format_exc())
    except Exception:
        pass
    _INSTALL_RESULT = "install-failed"
