#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fxlong — 长跑脚本用的 edit 通信层：与 fxedit 同链路，但不删除 ipc/out 回执。

为什么需要它（2026-09-19 两次被静默终止后定位）：
  cli/fxedit.py::call_edit 每调用一次就 os.remove 一个 ipc/out 回执文件，
  几十次以上的循环会累计撞上宿主 safe-delete 批量阈值（50/turn）→ 进程被
  **静默终止**（退出码 1、无 traceback，日志只剩一行
  `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`）。
  本模块底层改用 cli/fxcall.py::call（**只读回执、不删文件**），链路与 MCP 一致。

  更新 2026-09-19：cli/fxedit.py 也已不再删回执（归拢改由 cli/fxsweep.py 改名归档），
  两条路径都不会再触发该钩子。本模块保留，差别在于 call_edit 会把 ok/value/result
  逐层剥到底（fxedit 返回带壳的 dispatch 信封），且出错时抛 RuntimeError 而非返回
  error 字段 —— 换 import 时要按这两点改调用方。

⚠️ 跑脚本时务必 `python -u xxx.py > run.log 2>&1`：管道（如 `| tail`）下 stdout 是
全缓冲，进程被终止时输出会整块丢失，看起来像「只跑了几行」。

用法:
  from fxlong import call_edit, eval_json
  r = call_edit('place_item', {'item_id': 202009, ...})   # 已剥壳到业务结果
  obj = eval_json("import json\\n__result__ = json.dumps({'a': 1})")
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# 2026-09-22 通道收敛：剥壳与下发逻辑统一到 cli/fxapi.py（唯一真源），
# 本模块保留原有函数签名与语义（业务值层 / 出错抛 RuntimeError），改为委托。
# 行为与旧实现严格等价：返回业务值、错误文案一致、同样不删除 ipc/out 回执。
from fxapi import call as _api_call, unwrap, envelope_value  # noqa: E402,F401
from fxapi import eval_json  # noqa: E402,F401

BRIDGE_DIR = os.path.join(ROOT, "bridge")


def call_edit(op, args=None, timeout=120.0):
    """与 cli/fxedit.py::call_edit 同链路，但不删除 ipc/out 回执文件。

    返回已剥壳的业务结果；桥接无响应或 op 报错时抛 RuntimeError。
    """
    return _api_call(op, args, timeout)
