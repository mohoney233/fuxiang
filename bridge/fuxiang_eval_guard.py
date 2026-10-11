"""eval 危险调用拦截名单 —— **两层共用的唯一真源**（2026-09-22）。

背景：`bridge/fuxiang_bridge.py::op_eval`（底层，fxcall/fxrun/probe 直连路径）与
`bridge/fuxiang_edit.py::op_eval`（上层，经 dispatch 路径）各有一份黑名单，
但**桥层只有 3 条、edit 层有 5 条** —— 缺 `get_submesh_bounding_box` 与
`get_submesh_bounding_radius`。结果：经 `cli/fxcall.py` / `cli/fxrun.py` 直发 eval
可绕过上层拦截，调用这两个接口会**冻结游戏进程**（实测不再返回、ping 无回包）。

本模块无第三方依赖、无 import 副作用，供两层同时引用，杜绝再次漂移。

⚠️ 本模块被常驻桥接进程加载；修改后需重新注入桥接
   （`fuxiang-inject.exe install`）才会生效。
"""

# (代码片段, 拦截原因)
EVAL_BLOCK_PATTERNS = (
    ("get_vertex_bounding", "主线程调用会冻结游戏（引擎文档明示不适合游戏进行时使用）"),
    ("gc.get_objects", "全堆扫描在本进程内会 MemoryError / 长时间卡顿"),
    # 2026-09-17 实测：model.get_socket_obj_file(i) 在编辑器主线程内阻塞且不释放 GIL，
    # 心跳线程随之饿死，dwrg.exe 整体无响应（ping 无回包），需重启游戏。
    # get_socket_count / get_socket_name 可安全调用。
    ("get_socket_obj_file", "主线程调用会阻塞并持 GIL 冻结游戏（2026-09-17 实测）"),
    # 2026-09-17 实测：model.get_submesh_bounding_box(i) 在编辑器主线程内调用后
    # 不再返回，心跳线程被饿死、ping 无回包，dwrg.exe 进程仍在但整体无响应。
    # 与 get_vertex_bounding 同族（逐子网格/顶点级回读），均不适合游戏进行时调用。
    ("get_submesh_bounding_box", "主线程调用会冻结游戏（2026-09-17 实测，与 get_vertex_bounding 同族）"),
    ("get_submesh_bounding_radius", "同族子网格回读接口，风险同上，一并拦截"),
)


def find_blocked(code):
    """返回 (命中片段, 原因)；未命中返回 None。"""
    if not code:
        return None
    for pattern, reason in EVAL_BLOCK_PATTERNS:
        if pattern in code:
            return pattern, reason
    return None
