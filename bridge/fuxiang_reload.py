#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smart_reload — 按 mtime+size 判断是否真的需要 reload。

背景（2026-09-19 实测）
---------------------
每次 `call_edit` 都会无条件 reload `fuxiang_edit`（23.7ms）、`fuxiang_scene`（10.2ms）、
`fuxiang_placement`（4.8ms），合计 **~39ms**；而真正的 `dispatch` 只要 **0.015ms**。
即模块热重载的固定成本比它保护的东西贵了 2000 倍。

本模块保留「改完即生效」的语义（文件 mtime/size 一变就 reload），只是去掉
「文件没变也照reload一遍」的浪费。

用法
----
    from fuxiang_reload import smart_reload
    scene = smart_reload("fuxiang_scene")

注意
----
* 本模块自身**不依赖任何其他 bridge 模块**（避免循环 import），只用标准库。
* 若本模块自己被 reload，`_STAMP` 缓存清空 → 下次调用各模块会各多 reload 一次，
  之后恢复缓存，行为仍然正确。
* 用 `st_mtime_ns`（纳秒）+ `st_size` 组合，避免粗粒度 mtime 在同一秒内改两次时漏判。
"""
import importlib
import os
import sys

_STAMP = {}


def smart_reload(name):
    """文件未变则返回缓存模块；变了才 reload。返回模块对象。"""
    m = sys.modules.get(name)
    if m is None:
        return importlib.import_module(name)
    path = getattr(m, "__file__", None)
    if not path:
        return m
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return m
    if _STAMP.get(name) == stamp:
        return m
    reloaded = importlib.reload(m)
    _STAMP[name] = stamp
    return reloaded


def force_reload(name):
    """无条件 reload（需要绕过缓存时显式调用）。"""
    m = sys.modules.get(name)
    if m is None:
        return importlib.import_module(name)
    reloaded = importlib.reload(m)
    path = getattr(reloaded, "__file__", None)
    if path:
        try:
            st = os.stat(path)
            _STAMP[name] = (st.st_mtime_ns, st.st_size)
        except OSError:
            _STAMP.pop(name, None)
    return reloaded


def stamps():
    """当前缓存快照（诊断用）。"""
    return dict(_STAMP)
