"""Nonblocking server creation followed by native editor confirmation.

State belongs to the current room, so reloading bridge modules cannot resend a
request. A timeout is an unknown outcome, never permission to create again.
The game protocol has no request ID: callers must not drag new objects while
an API placement is pending. Ambiguous responses are left untouched.
"""
import time
import uuid

# 上限只约束**未定论**请求（pending / send_unknown / overdue / ambiguous /
# conflict / finalize_failed，以及正锁定「单笔在途」的那一条）。已定论且已解除
# 锁定的请求（目前只有 placed）会被 _prune 搬进 settled 紧凑留痕表，因此长会话
# 反复摆放不会再撞上限，而幂等回查语义保持不变。
MAX_REQUESTS = 128
# 留痕表只存 id / status / spec / result 四项纯数据，不含 ui、cm 及大集合引用；
# 超限按插入序 FIFO 淘汰最旧的，够覆盖「调用方回头用旧 request_id 重试」的窗口。
MAX_SETTLED = 1024


def _state(room):
    state = vars(room).setdefault(
        '_fuxiang_placements', {'requests': {}, 'active': None})
    # 兼容热重载前建立的旧 room 状态（当时没有 settled 键）。
    state.setdefault('settled', {})
    return state


def _summarize(req):
    """把已定论的请求压成紧凑摘要：只留回查所需字段，解除对编辑器对象的强引用。"""
    return {'id': req['id'], 'status': req.get('status'),
            'spec': req.get('spec'), 'result': req.get('result')}


def _settled_ok(req):
    """这次请求是否"摆放成功且回执完整" —— 只有这种才允许释放单笔在途锁。

    ⛔ 不能宽到「有 result 就算定论」：`ambiguous / conflict / finalize_failed /
    send_unknown / overdue` 都带 result，但它们**故意**留着锁（见 begin 的报错文案），
    逼调用方先检查现场，否则会在半个成功的物件上再叠一件。
    """
    return ((req or {}).get('result') or {}).get('status') == 'placed'


def _prune(state):
    """回收已定论请求，使 MAX_REQUESTS 只统计未定论数。

    active 指向的请求必须留下：`begin` 靠它维持「单笔在途」锁并复现
    「前一摆放请求尚未解决」的报错，`poll` 也靠它继续查询迟到结果。
    ⚠️ 唯一例外是**摆放成功**（`_settled_ok`）的请求：它的锁本来就该解除。
    2026-10-01 实机栽过：挂在全局单元上的组件（运动器 203911）走
    `_confirm_global_component` 后只写了 result、没同步 `req['status']`，
    于是 active 永远指着它 ⇒ 同批下一件摆放被「前一摆放请求尚未解决」顶掉，
    而春岚 332 个运动器会先把 128 的未定论额度吃满、把全量还原拦腰截断。
    迟到查询不受影响：`poll` 会回落到 settled 拿同一份回执。

    2026-10-02 批量化：回收判据从「有 result」收紧为「**placed** 才回收」
    （`_settled_ok`）。护栏 3 的字面语义——只有 placed 释放额度；
    `finalize_failed` 等带 result 的失败定性必须留在 requests 里继续占额，
    逼调用方先检查现场（旧单笔路径里它们被 active 锁住、从不可回收，
    批量路径没有单指针，必须在判据里显式排除）。迟到的失败回执仍可
    经 requests / settled 查到，语义不变。
    """
    requests = state['requests']
    settled = state.setdefault('settled', {})
    active = state.get('active')
    if active is not None:
        cur = requests.get(active)
        if cur is None or _settled_ok(cur):
            state['active'] = active = None
    for token in [t for t, r in requests.items()
                  if t != active and _settled_ok(r)]:
        settled[token] = _summarize(requests.pop(token))
    while len(settled) > MAX_SETTLED:
        settled.pop(next(iter(settled)))


def _as_int(key):
    """组件表 key 可能是 int / 数字字符串 / 其它；统一成 int，失败返回 None。"""
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


UI_CUSTOM_COMPONENT_TYPE = 200000     # 界面编辑器的控件（UGCUiCustomComponent）


def _try_num(obj, name):
    fn = getattr(obj, name, None)
    try:
        return fn() if callable(fn) else None
    except Exception:
        return None


def _ui_view_open(ui):
    """界面编辑器是否在前视图 —— 只有开着时原生的 UI 创建命令才非空。"""
    try:
        return bool(ui._get_is_show_custom_ui_view())
    except Exception:
        return False


def _is_ui_widget(req, comp=None):
    """本次请求摆的是不是界面控件（组件类型 200000）—— 决定走哪条原生确认路径。

    以 spec 的组件类型为准（来自物件目录），再核组件自身的 COMPONENT_TYPE：
    两者不一致说明目录与实体的类型不同步（改版/串请求），宁可走通用路径。
    """
    if (req.get('spec') or {}).get('component_type') != UI_CUSTOM_COMPONENT_TYPE:
        return False
    if comp is None:
        return True
    for base in type(comp).__mro__:
        ct = getattr(base, 'COMPONENT_TYPE', None)
        if ct is not None:
            return _as_int(ct) == UI_CUSTOM_COMPONENT_TYPE
    return True


def _new_global_components(cm, req, item_id):
    """第二种匹配口径：服务端创建了组件但没有新实体（组件挂在 UgcGlobalUnit 上）。

    2026-09-19 实测：203911 运动器 / 203930 坐标中心偏移 / 203920 音乐事件 /
    203921 镜头位置 / 10007001 剧情 / 200001(6000001) 技能控件 / 200000(5101001) 环1
    都会命中这里——此前 place_poll 只会一直 pending，被误判为「类型不可用」。
    只接受 owner 的 ugc_type 与请求 item_id 一致的新组件，避免误匹配别人的请求。
    """
    before_ids = req.get('before_ids') or set()
    out = []
    for key, comp in cm._ugc_id2component.items():
        ugc_id = _as_int(key)
        if ugc_id is None or ugc_id in before_ids:
            continue
        unit = getattr(comp, '_owner', None)
        if unit is None:
            continue
        try:
            if int(getattr(unit, 'ugc_type', 0) or 0) != int(item_id):
                continue
        except (TypeError, ValueError):
            continue
        out.append((unit, comp))
    return out


def _reply(req, poll_after_ms=None):
    if req.get('result') is not None:
        # 回执就是定论：把请求自身的 status 也对齐。否则会出现「已 placed 却仍写着
        # pending」的记录 —— 2026-10-01 实机：这种记录既骗过「单笔在途」判据（把同批
        # 下一件摆放顶掉），也让 MAX_REQUESTS 的未定论统计虚高（春岚 332 个运动器
        # 会先把 128 的额度吃满，全量还原中途崩）。
        st = (req['result'] or {}).get('status')
        if st and req.get('status') != st:
            req['status'] = st
        out = dict(req['result'])
        # 抄自快照的缩放落在编辑器滑块范围外（组合件在春岚就是这样），摆放成功也要
        # 把越界值显式带回来 —— 引擎照收，但事后排查"尺寸不对"时这是唯一线索。
        dev = (req.get('spec') or {}).get('scale_out_of_catalog_range') or []
        if dev and out.get('status') == 'placed':
            out['catalog_scale_deviation'] = dev
        return out
    if poll_after_ms is None:
        # 2026-09-19 实测（10/10 样本，place 返回到 placed 中位 180ms）：place 之后
        # **立即** poll 即得 placed。旧的固定 500ms 建议值只是保守猜测，会让每次摆放
        # 白等 0.35~0.5s（实测占单次耗时 55%）。故：刚发出 → 0（立即查）；
        # 已查过一次仍 pending → 150ms 退避，避免调用方忙轮询。
        poll_after_ms = 150 if req.get('polled') else 0
    return {'request_id': req['id'], 'status': req['status'],
            'scope': 'server', 'persistence_verified': False,
            'elapsed_s': round(time.monotonic() - req['started'], 2),
            'poll_after_ms': poll_after_ms,
            'note': req.get('note', '请求已发送；请**立即**调用 fuxiang_place_poll 查询'
                                    '（实测首次查询通常已 placed）；若仍 pending，'
                                    '再按 poll_after_ms 退避重试。等待期间请勿拖入新物件。')}


def begin(scene, edit, args, spec, room, cm, ui):
    state = _state(room)
    _prune(state)
    token = args.get('request_id')
    if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 128):
        raise ValueError('request_id 必须为 1 至 128 字符的字符串')
    if token in state['requests']:
        req = state['requests'][token]
        if req['spec'] != spec:
            raise ValueError('同一 request_id 不可用于不同的摆放参数')
        if _receipt_alive(req, cm):
            return _reply(req)
        # 旧 run 的残影（组件已不在图上）⇒ 不是幂等命中，摘掉后重新登记
        state['requests'].pop(token, None)
        if state.get('active') == token:
            state['active'] = None
    settled = state['settled'].get(token) if token is not None else None
    if settled is not None:
        # 已定论的历史请求：沿用同一编号仍然幂等返回，绝不复用编号重建物件。
        if settled['spec'] != spec:
            raise ValueError('同一 request_id 不可用于不同的摆放参数')
        if _receipt_alive(settled, cm):
            return _reply(settled)
        # 同上：回执指向的组件已经不在了（跨 run 换 progress 重跑就会碰到），
        # 留着它会让调用方"跳过摆放却按旧 id 写属性"，必须重新登记。
        state['settled'].pop(token, None)
    # 「单笔在途」判据（2026-10-02 批量化）：从 active 单指针推广为「任一未定论
    # 请求」（per-token 账本，护栏 2）。同规格的未定论请求**幂等返回**——重试同一
    # spec 不许建两件（去重判据 = spec 等值查未定论请求，不再只看 active 单指针）；
    # 不同规格则报错点名最老的未定论请求，逼调用方先查询/检查现场再继续。
    # 纯单笔会话里未定论请求恰好只有 active 一条，行为与旧判据逐字一致。
    unsettled = [(t, r) for t, r in state['requests'].items() if not _settled_ok(r)]
    if unsettled:
        if token is None:
            for _t0, r0 in unsettled:
                if r0['spec'] == spec:
                    return _reply(r0)
        raise ValueError('前一摆放请求尚未解决，请继续查询 request_id=%s；不要重复创建'
                         % unsettled[0][0])
    if ui.ugc_params.get('is_dragging_create'):
        raise ValueError('编辑器正在拖拽创建，请结束当前操作后再调用')
    # The old whitelist covers client-local construction only. Complex native
    # placements may create multiple units and need dedicated confirmation.
    if spec['component_type'] not in scene.SERVER_COMPONENT_TYPES:
        # 2026-09-19 修正文案：白名单已扩至 66 类（原「只支持 100000/202014」是
        # 客户端保守阀门的旧描述，会误导调用方以为服务端按类型拒绝）。
        ct = spec['component_type']
        raise ValueError(
            '组件类型 %s 未列入客户端已实测的服务端摆放白名单（共 %d 类，见 '
            'fuxiang_scene.SERVER_COMPONENT_TYPES）。这不是服务端限制——服务端 '
            'add_unit 无类型白名单，唯一判据是 place_poll 是否 placed。'
            '确要尝试：在 fuxiang_eval 内临时扩展 SERVER_COMPONENT_TYPES 后摆放，'
            '或改用 mode=local_preview（不持久化）。'
            % (ct, len(scene.SERVER_COMPONENT_TYPES)))
    if len(state['requests']) >= MAX_REQUESTS:
        raise ValueError(
            '本会话未定论请求已达 %d 条。已定论（placed 等）的记录已自动回收进留痕表，'
            '不计入此上限，同一 request_id 仍可幂等回查；占额的是 '
            'ambiguous / conflict / overdue / send_unknown / finalize_failed '
            '这类会锁住会话又不自动释放的记录。请先按 place_poll 的 note 检查现场，'
            '或保存并重进地图（room 重建即归零）。' % MAX_REQUESTS)
    token = token or uuid.uuid4().hex
    req = {'id': token, 'status': 'pending', 'started': time.monotonic(),
           'spec': spec, 'ui': ui, 'cm': cm,
           'before_uids': {getattr(c, '_owner_uid', None) for c in cm._ugc_id2component.values()},
           # 2026-09-19：部分类型（运动器 203911 / 音乐事件 203920 / 剧情 10007001 等）
           # 服务端只创建**组件**并挂在（既有或新建的）UgcGlobalUnit 上，不产生新实体，
           # 因此必须同时快照组件 id，poll 才有第二种匹配口径。
           'before_ids': {_as_int(k) for k in cm._ugc_id2component}}
    state['requests'][token] = req
    state['active'] = token
    # 判_role 必须在 try 之外：放在里面会让代码自身的 AttributeError 被下面那条
    # except 吞成「发送异常，结果未知」，把一次编程错误记成不可重发的在途请求。
    is_role_begin = spec['component_type'] in scene.ROLE_COMPONENT_TYPES
    try:
        if is_role_begin:
            # 角色只有在**创建那一步携带坐标**时才会把落位持久化
            # （`_add_unit_server` 的可选 `pos` 随 add_unit 一起上送；
            # 创建后再 set_position 会被服务端拉回哨兵位 —— EDIT_OPS.md §11.1）
            ui._add_unit_server([spec['item_id']], pos=tuple(spec['position']))
        else:
            ui._add_unit_server([spec['item_id']])
    except Exception as exc:
        req['status'] = 'send_unknown'
        req['note'] = '发送异常，结果未知；保留请求以避免重复创建: %s' % exc
    return _reply(req)


def _confirm_global_component(scene, req, state, ui, pair):
    """确认「只产生组件、无独立实体」的服务端创建结果。

    2026-09-19 实测：对这类组件调用 `ui._add_change_unit(owner.uid)` 会把组件
    ugc_id 登记进 `change_units['component']`（引擎自动分桶），保存即随变更集上送。
    若 owner 是本次新建的全局单元（uid 不在 before_uids 且仍在 create_units 中），
    先走一次原生确认再登记。

    2026-09-21 重大修正（剧情/区域等内容重进全丢的根因）：
      服务端 add_unit 同步时会把新组件的 ugc_id 放进 ``room.del_unit_uids['component']``
      （待确认/待删除态）；原生命令层确认（_normal_create_units）只清 'unit' 桶，
      **从不清 'component' 桶**。于是保存时 `_get_change_uid → get_del_unit_uids`
      把这些组件当**删除**上送（插桩实证：del_uid.component=[353,354,355]），
      服务端创建后又收到删除 → 保存落库为空 → 重进全丢。
      确认完成后必须显式 ``room.change_del_unit_uids(False, ugc_id, 'component')``
      摘除待删除标记（单元桶也一并清一次，幂等）。
    """
    unit, comp = pair
    spec = req['spec']
    room = None
    try:
        room = unit.room
    except Exception:
        room = None
    try:
        try:
            p = unit.get_position3()
            pos = [float(p.x), float(p.y), float(p.z)]
        except Exception:
            pos = None
        uid_is_new = unit.uid not in (req.get('before_uids') or set())
        if uid_is_new and any(x is unit for x in (ui.ugc_params.get('create_units') or [])):
            ok, error = scene.command_create(ui, unit, pos or [0, 0, 0])
            if not ok:
                raise RuntimeError(error or '原生确认失败')
        ok, error = scene.register_change(ui, unit.uid)
        if not ok:
            raise RuntimeError(error or '登记保存变更集失败')
        scene.unregister_create(ui, unit, comp)
        # 摘除「待删除」标记：不清这里，保存会把组件当删除上送（见函数注释）
        if room is not None and hasattr(room, 'change_del_unit_uids'):
            try:
                room.change_del_unit_uids(False, comp._ugc_id, 'component')
            except Exception:
                pass
            try:
                room.change_del_unit_uids(False, unit.uid, 'unit')
            except Exception:
                pass
        req['result'] = {'request_id': req['id'], 'status': 'placed', 'scope': 'server',
                         'ugc_id': comp._ugc_id, 'owner_uid': unit.uid,
                         'item_id': spec['item_id'],
                         'entity_mode': 'global_component',
                         'unit_class': type(unit).__name__,
                         'component_class': type(comp).__name__,
                         'transform': {'position': pos, 'rotation': None, 'scale': None},
                         'editor_registered': True, 'persistence_verified': False,
                         'component_count': ui.get_all_ugc_component_nums(),
                         'occupancy': ui.get_current_occupancy(),
                         'note': ('服务端已创建组件并挂在 %s 上（该类无独立实体、不占占用值）；'
                                  '已登记保存变更集，持久化须保存并重进地图核实。'
                                  % type(unit).__name__)}
        state['active'] = None
    except Exception as exc:
        # 与实体确认一致：不自动重试，保留现场。
        req['result'] = {'request_id': req['id'], 'status': 'finalize_failed',
                         'error': str(exc), 'owner_uid': unit.uid, 'ugc_id': comp._ugc_id,
                         'entity_mode': 'global_component', 'scope': 'server',
                         'persistence_verified': False,
                         'note': '可能已部分确认；已停止自动重试，保留现场供检查。'}
    return _reply(req)


def _ui_widget_pos(comp):
    """读控件属性袋里的 POS(20000010001) —— 确认成功的直接证据。"""
    try:
        for e in (comp.component_info or {}).get('events') or []:
            if str(e.get('type')) == '20000010001':
                info = e.get('info')
                return [float(info[0]), float(info[1])] if isinstance(info, (list, tuple)) \
                    and len(info) >= 2 else None
    except Exception:
        return None
    return None


def _component_type(comp):
    for base in type(comp).__mro__:
        ct = getattr(base, 'COMPONENT_TYPE', None)
        if ct is not None:
            return _as_int(ct)
    return None


def _new_ui_components(cm, req):
    """请求之后**新出现的界面控件组件** [(ugc_id, comp, unit)]。

    为什么不能沿用另外两种口径（2026-09-23 实机）：图上的界面控件**共用同一个**
    UgcGlobalUnit（实测 362/363/364/365 全在 uid 1000313 上，`unit.ugc_type` 只是
    第一件的 item_id），所以 `candidates`（要新实体）与 `_new_global_components`
    （要 owner.ugc_type == 请求 item_id）都匹配不到第 2..N 件，只能按组件表差集认领。
    实测一次请求建**一件**（5101000 → 只多出一个组件）；这里仍按列表确认，因为原生
    `on_check_ui_component_limit` 收的就是列表，多出的件一起处理比逐件重发安全。
    ⚠️ 代价：等待期间玩家手动拖入的同类控件会被一并认领 —— 摆放协议本就要求
    「一笔在途期间请勿拖入新物件」，回执里如实给出 ugc_ids / widget_item_ids 供核对。
    """
    before = req.get('before_ids') or set()
    out = []
    for key, comp in cm._ugc_id2component.items():
        gid = _as_int(key)
        if gid is None or gid in before:
            continue
        if _component_type(comp) != UI_CUSTOM_COMPONENT_TYPE:
            continue
        unit = getattr(comp, '_owner', None)
        if unit is None:
            continue
        out.append((gid, comp, unit))
    return sorted(out)


def _ui_item_id(room, gid, comp):
    """控件真实是哪件：`room.get_component_item_id(ugc_id)`（单元上的 ugc_type 不可信）。"""
    fn = getattr(room, 'get_component_item_id', None)
    try:
        if callable(fn):
            return _as_int(fn(gid))
    except Exception:
        pass
    return _as_int(getattr(comp._owner, 'ugc_type', None))


def _confirm_ui_component(scene, req, state, ui, comps):
    """界面控件（组件类型 200000 UI_CUSTOM）的原生确认；comps = [(ugc_id, comp, unit), ...]。

    照抄调色板拖放落地 `UIBattleUGCRightComponentView._handle_2d_end` 的
    `is_leave_option` 分支（2026-09-23 实机逆向）：

        ui.on_check_ui_component_limit([comp], end_pos, update_params)

    `end_pos` 只用到 `.x` / `.y`（画布 2D 坐标，设计尺寸 1334×750，原点左下，
    与场景的 [x,y,z] 无关）；`update_params` 四个键与原生一致。该调用内部先跑
    `ui_component_detector_chain`（占用值校验，超预算时可能弹窗），通过后由
    `_get_create_ui_components_commands` 完成 `_add_change_unit(unit.uid)` +
    `change_component_id(ugc_id)` + `UGCUIObjCommand(view='UIBattleUGCCustomUI')`
    + `UGCObjChangeCommand(type=True)` + `UGCOccupancyChangeCommand(type=True)`
    + 把 end_pos 写进 POS 槽位，最后 `iUIBattleUGCCustomUICommandControl.do_command`
    进撤销栈与保存变更集 —— 登记/占用/坐标一并到位，这里**不再**自己 reg_change。

    ⚠️ 前置条件：`ui._get_is_show_custom_ui_view()` 必须为真。该命令构建器在界面
    编辑器没打开时**直接返回空命令**（表现为"调用成功但什么都没发生"，控件留在
    待删除袋里，保存时被当删除上送）。因此 `poll` 只在视图打开时才分流到这里，
    关着时沿用 `_confirm_global_component`（历史上 200000 图标类件的既有路径）。
    `fuxiang_ui_edit.op_ui_add` 则要求必须打开，否则直接报错。
    """
    spec = req['spec']
    comps = list(comps or [])
    if not comps:
        raise ValueError('_confirm_ui_component 需要至少一个控件组件')
    _gid0, first_comp, unit = comps[0]
    try:
        room = unit.room
    except Exception:
        room = None
    # 主件 = 这次请求点名的那件（万一同时冒出不属于本请求的同类件，回执里也照实列出）
    item_of = {gid: _ui_item_id(room, gid, c) for gid, c, _u in comps}
    primary = next((t for t in comps if item_of[t[0]] == spec['item_id']), comps[0])
    pgid, pcomp, punits = primary[0], primary[1], primary[2]
    result = {'request_id': req['id'], 'scope': 'server', 'item_id': spec['item_id'],
              'ugc_id': pgid, 'ugc_ids': sorted(item_of), 'owner_uid': punits.uid,
              'widget_item_ids': {str(k): v for k, v in sorted(item_of.items())},
              'entity_mode': 'ui_widget', 'unit_class': type(punits).__name__,
              'component_class': type(pcomp).__name__,
              'persistence_verified': False}
    try:
        try:
            open_view = bool(ui._get_is_show_custom_ui_view())
        except Exception as exc:
            raise RuntimeError('读不到界面编辑器视图状态（_get_is_show_custom_ui_view）: %s' % exc)
        if not open_view:
            raise RuntimeError('界面编辑器没在前视图：原生创建命令会返回空，控件会留在待删除态。'
                               '请在游戏内切到界面编辑器后重试（本请求不会被自动重发）。')
        try:
            import math3d
            p = spec['position']
            end_pos = math3d.vector(float(p[0]), float(p[1]), 0.0)
        except Exception as exc:
            raise RuntimeError('构造画布坐标失败: %s' % exc)
        # 一次调用整组确认（与玩家拖放同形：原生收的是组件列表）
        ui.on_check_ui_component_limit(
            [c for _g, c, _u in comps], end_pos,
            {'refresh_occupancy': True, 'refresh_component_nums': True,
             'refresh_ui_custom': True, 'select_ugc_id': pgid})
        # 回读校验：主件的 POS 必须落地且等于请求坐标
        pos_now = _ui_widget_pos(pcomp)
        if pos_now is None:
            raise RuntimeError('确认后主件 %s 的属性袋里没有 POS(20000010001)：原生确认疑似未生效'
                              '（占用值校验弹窗？界面编辑器视图？）' % pgid)
        if any(abs(a - b) > 0.51 for a, b in zip(pos_now, [float(spec['position'][0]),
                                                           float(spec['position'][1])])):
            raise RuntimeError('确认后控件坐标与请求不一致: %s vs %s' % (pos_now, spec['position']))
        pending = [str(x) for x in ((getattr(room, 'del_unit_uids', None) or {})
                                    .get('component') or [])] if room is not None else []
        for gid, comp, _u in comps:
            if str(gid) not in pending:
                continue
            # 原生命令没摘待删除标记时显式摘除：留着它，保存会把刚建的控件当删除上送
            # （与 _confirm_global_component 里 2026-09-21 那条根因同一通道）
            try:
                room.change_del_unit_uids(False, gid, 'component')
            except Exception as exc:
                raise RuntimeError('待删除标记摘不掉（ugc_id=%s）: %s，保存会删掉它' % (gid, exc))
            if str(gid) in [str(x) for x in ((getattr(room, 'del_unit_uids', None) or {})
                                             .get('component') or [])]:
                raise RuntimeError('待删除标记摘不掉（ugc_id=%s），保存会删掉它' % gid)
        try:
            room.change_del_unit_uids(False, punits.uid, 'unit')
        except Exception:
            pass
        for gid, comp, u in comps:
            scene.unregister_create(ui, u, comp)
        cu = (getattr(ui, 'ugc_params', None) or {}).get('change_units') or {}
        registered = any(str(x) == str(punits.uid) for x in (cu.get('unit') or [])) or \
            any(str(x) in {str(g) for g in item_of} for x in (cu.get('component') or []))
        result.update({
            'status': 'placed', 'transform': {'position': pos_now, 'rotation': None,
                                              'scale': None},
            'canvas_position': pos_now,
            'editor_registered': registered,
            'component_count': _try_num(ui, 'get_all_ugc_component_nums'),
            'occupancy': _try_num(ui, 'get_current_occupancy'),
            'note': ('界面控件已由原生 on_check_ui_component_limit 整组确认（登记+占用+坐标）。'
                     '本次确认的件都在 ugc_ids 里（哪件是哪个 item 见 widget_item_ids）。'
                     '须游戏内保存并重进核实；控件不在 all_ugc_uid_set、也不各占一个单元，均属正常。')})
        state['active'] = None
    except Exception as exc:
        result.update({'status': 'finalize_failed', 'error': str(exc),
                       'note': '可能已部分确认；已停止自动重试，保留现场（用 ui_edit_verify '
                               '看这些控件是否进报文、是否仍待删除）。'})
        req['result'] = result
        return _reply(req)
    req['result'] = result
    return _reply(req)


def _finalize_unit(scene, ui, room, unit, comp, tr, token, is_role=False):
    """按玩家手动放置的同一条原生路径确认**一个**新实体。

    `tr` 是这一件该落的变换（主件用 spec，成组创建的第 2..N 件用 group_transforms），
    回读严格按 `tr` 校验：摆歪了要当场炸，不能让一次批量还原带着错坐标往下跑。
    角色类例外 —— `get_position3()` 在未落位态给哨兵，回读要认 `unit.position`。
    """
    if is_role:
        scene.apply_role_transform(unit, tr)
    else:
        scene._apply_full_transform(unit, tr)
    ok, error = scene.command_create(ui, unit, tr['position'])
    if not ok:
        raise RuntimeError(error)
    if is_role:
        # 取消「半隐藏/未落位」态，否则角色碰撞不生效且 Y 停在负一万量级（§11.2）
        unit.hide_collision_pos(False)
    if unit.uid not in ui.all_ugc_uid_set or unit.uid in room.del_unit_uids.get('unit', []):
        raise RuntimeError('原生确认后对象登记仍不完整')
    actual = scene.transform(unit)
    if is_role:
        actual['position'] = scene.role_position(unit)
    for key in ('position', 'rotation', 'scale'):
        if actual.get(key) is None:
            # 2026-09-19：部分实体类无 ugc_dir/ugc_size 读属性，无法回读，
            # 跳过该维度校验（位置仍严格校验）。
            continue
        for got, want in zip(actual[key], tr[key]):
            delta = (got-want+180) % 360-180 if key == 'rotation' else got-want
            if abs(delta) > .001:
                raise RuntimeError('确认后的变换不一致: %s' % key)
    unit._fuxiang_local_item = {'ugc_id': comp._ugc_id, 'item_id': tr['item_id'],
                                'server_created': True, 'request_id': token}
    scene.unregister_create(ui, unit, comp)
    return actual


def _confirm_group_create(scene, req, state, ui, room, token, candidates, group_size):
    """一次 `add_unit` 生成多个实体的类型（204041 推荐检查点成对）：整组认领、逐件确认。

    实机 2026-10-01：`add_unit(item 204041)` 一次产出 2 个 UgcLogicUnit，引擎自动把两件的
    `component_info['group_components']` 写成互指 ⇒ 成组由创建本身建立。旧代码在这里判
    `candidates != 1` 直接 ambiguous，于是这件**永远摆不出来**（春岚那 2 件推荐检查点）。
    回执里的 `group_ugc_ids` 按 uid 升序，还原侧用它把快照两件的旧 id 都映到新 id。
    """
    spec = req['spec']
    ordered = sorted(candidates, key=lambda p: p[0].uid)[:group_size]
    extras = spec.get('group_transforms') or []
    confirmed = []
    try:
        for idx, (unit, comp) in enumerate(ordered):
            tr = {'item_id': spec['item_id'], 'position': list(spec['position']),
                  'rotation': list(spec.get('rotation') or (0, 0, 0)),
                  'scale': list(spec.get('scale') or (1, 1, 1))}
            if idx:
                # 成员没有自己的变换就沿用主件的（两件叠在一处，人工再拖其中一件）。
                # 这不是假想分支：begin 存下的旧 spec 里可能根本没有 group_transforms
                # （字段是后加的），继续查询那种搁置请求时就走这条回落。
                extra = extras[idx - 1] if idx - 1 < len(extras) else None
                if extra:
                    tr.update({'position': list(extra['position']),
                               'rotation': list(extra['rotation']),
                               'scale': list(extra['scale'])})
            actual = _finalize_unit(scene, ui, room, unit, comp, tr, token)
            confirmed.append({'ugc_id': comp._ugc_id, 'owner_uid': unit.uid,
                              'transform': actual})
    except Exception as exc:
        # 原生命令可能只跑了一半：绝不重放，也不本地销毁服务端已拥有的对象。
        req['result'] = {'request_id': token, 'status': 'finalize_failed',
                         'error': str(exc), 'scope': 'server',
                         'confirmed_before': [c['ugc_id'] for c in confirmed],
                         'persistence_verified': False,
                         'note': '成组创建只确认了前面几件；已停止自动重试，保留现场供检查。'}
        return _reply(req)
    req['result'] = {'request_id': token, 'status': 'placed', 'scope': 'server',
                     'ugc_id': confirmed[0]['ugc_id'],
                     'owner_uid': confirmed[0]['owner_uid'],
                     'group_ugc_ids': [c['ugc_id'] for c in confirmed],
                     'group_owner_uids': [c['owner_uid'] for c in confirmed],
                     'item_id': spec['item_id'], 'transform': confirmed[0]['transform'],
                     'editor_registered': True, 'persistence_verified': False,
                     'catalog_hidden': spec.get('catalog_hidden', False),
                     'object_list_eligible': spec.get('object_list_eligible', True),
                     'component_count': ui.get_all_ugc_component_nums(),
                     'occupancy': ui.get_current_occupancy(),
                     'note': ('成组创建（一次 add_unit 出 %d 件，%s）：已逐件原生确认。'
                              '引擎自动写好两件的 group_components 互指 ⇒ 成组不需要人工框选。'
                              '持久化须保存并重进地图核实。'
                              % (group_size, spec['component_type']))}
    state['active'] = None
    return _reply(req)


def poll(scene, edit, args):
    room, cm = scene.context(edit)
    state = _state(room)
    token = args.get('request_id')
    req = state['requests'].get(token) if isinstance(token, str) else None
    if req is None:
        settled = state['settled'].get(token) if isinstance(token, str) else None
        if settled is not None:
            # 已定论请求的回查：返回定论本身，不重放任何原生命令。
            return _reply(settled)
        raise ValueError('当前地图会话没有此 request_id；重进后旧请求不可继续')
    if req.get('result') is not None:
        return _reply(req)
    # 打过一次仍 pending：后续回复改用退避间隔，避免调用方忙轮询。
    req['polled'] = True
    if req['status'] in ('ambiguous', 'conflict'):
        # ambiguous 的语义是「未移动或确认任何一个」⇒ 重估是安全的。成组创建的
        # 类型（GROUP_CREATE_TYPES）尤其需要：旧版本按"候选数≠1"判的 ambiguous，
        # 物件其实还挂在 create_units 里等认领，不开这条路就只能重进地图才能收拾现场。
        # conflict 可能已经碰过现场（玩家同时手动放置），仍旧保守拒绝。
        if (req['status'] == 'ambiguous'
                and scene.GROUP_CREATE_TYPES.get(
                    (req.get('spec') or {}).get('component_type'))):
            req['status'] = 'pending'
        else:
            return _reply(req)
    ui = scene.get_edit_ui(edit)
    if ui is not req['ui'] or cm is not req['cm']:
        raise ValueError('编辑器上下文已更换，未继续操作旧请求')
    return _poll_pending(scene, req, state, ui, cm, room, token)


def _poll_pending(scene, req, state, ui, cm, room, token, assigned=None):
    """推进一个 pending 请求：匹配候选并按类型走原生确认。

    `assigned=None` 时自行按 before 快照差集找候选（单笔路径）；
    `assigned=[]` 表示批量认领者这一轮没分到实体——直接落到等待/overdue 分支；
    `assigned=[(unit, comp), ...]` 为批量池化认领的结果（成组给满 group_size 个），
    直接进入实体确认，不再自行匹配。控件/全局组件的认领由 poll_batch 直接
    分流到各自的 _confirm_*，不经这里。
    """
    # 界面控件（UI_CUSTOM）：所有控件共用一个 UgcGlobalUnit，既不会出现在下面的
    # candidates（要求新实体）里，也不匹配 _new_global_components（要求 owner.ugc_type
    # == 请求 item_id）—— 只有这条按「请求后新出现的 200000 组件」认领的专路能找到它。
    # 视图没打开时不分流（原生确认会返回空命令），退回历史路径。
    if assigned is None and _is_ui_widget(req) and _ui_view_open(ui):
        ui_new = _new_ui_components(cm, req)
        if ui_new:
            return _confirm_ui_component(scene, req, state, ui, ui_new)
    candidates = list(assigned or [])
    if assigned is None:
        candidates = []
        for comp in cm._ugc_id2component.values():
            unit = getattr(comp, '_owner', None)
            if (unit is not None and unit.uid not in req['before_uids']
                    and getattr(unit, 'ugc_type', None) == req['spec']['item_id']):
                candidates.append((unit, comp))
    if not candidates and assigned is None:
        # 2026-09-19：没有新实体 ≠ 服务端不受理。先按「新组件」口径再找一次
        # （运动器/音乐事件/剧情/坐标偏移/技能控件/环1 等挂在全局单元上的组件）。
        gcomps = _new_global_components(cm, req, req['spec']['item_id'])
        if len(gcomps) == 1:
            return _confirm_global_component(scene, req, state, ui, gcomps[0])
        if len(gcomps) > 1:
            req['status'] = 'ambiguous'
            req['note'] = ('发现多个新组件（无独立实体、挂在全局单元上），无法对应请求；'
                           '未确认任何一个。这类类型建议一次只发一个请求。')
            return _reply(req)
        if time.monotonic() - req['started'] >= 180:
            req['status'] = 'overdue'
            req['note'] = '已等待 180 秒，结果仍未知；可继续查询原请求，不会自动重发。'
        return _reply(req)
    group_size = scene.GROUP_CREATE_TYPES.get(req['spec'].get('component_type'))
    if group_size and len(candidates) >= group_size:
        return _confirm_group_create(scene, req, state, ui, room, token,
                                     candidates, group_size)
    if len(candidates) != 1:
        req['status'] = 'ambiguous'
        req['note'] = '发现多个同类新物件，无法对应请求；未移动或确认任何一个。'
        return _reply(req)
    unit, comp = candidates[0]
    # 2026-09-19：全局/逻辑类实体（UgcGlobalUnit / UgcLogicUnit，如环1 5101001）
    # 不会被放进 del_unit_uids，按下面的「待确认态」判据必然落到 conflict。
    # 但只要它确实是本次新建、且未被编辑器登记（即不是玩家手动放置），
    # 就可以安全地退化为「登记保存变更集」的确认方式，而不是报错。
    if (unit.uid not in ui.all_ugc_uid_set
            and not ui.ugc_params.get('is_dragging_create')
            and not (unit.uid in room.del_unit_uids.get('unit', [])
                     and any(u is unit for u in (ui.ugc_params.get('create_units') or [])))
            and type(unit).__name__ in ('UgcGlobalUnit', 'UgcLogicUnit')):
        return _confirm_global_component(scene, req, state, ui, (unit, comp))
    # Native inbound handling puts a new unit in create_units and del_unit_uids.
    # Already-confirmed/manual placements must never be adopted by this API.
    if (unit.uid in ui.all_ugc_uid_set
            or unit.uid not in room.del_unit_uids.get('unit', [])
            or not any(u is unit for u in (ui.ugc_params.get('create_units') or []))
            or ui.ugc_params.get('is_dragging_create')):
        req['status'] = 'conflict'
        req['note'] = '候选物件不在独占的待确认状态；未改动。请检查是否同时进行了手动放置。'
        return _reply(req)
    req['status'] = 'finalizing'
    is_role = req['spec'].get('component_type') in scene.ROLE_COMPONENT_TYPES
    try:
        actual = _finalize_unit(scene, ui, room, unit, comp, req['spec'], token, is_role)
        req['result'] = {'request_id': token, 'status': 'placed', 'scope': 'server',
                         'ugc_id': comp._ugc_id, 'owner_uid': unit.uid,
                         'item_id': req['spec']['item_id'], 'transform': actual,
                         'editor_registered': True, 'persistence_verified': False,
                         'catalog_hidden': req['spec'].get('catalog_hidden', False),
                         'object_list_eligible': req['spec'].get('object_list_eligible', True),
                         'component_count': ui.get_all_ugc_component_nums(),
                         'occupancy': ui.get_current_occupancy(),
                         'note': '已完成原生确认；持久化须保存并重进地图核实。'}
        state['active'] = None
    except Exception as exc:
        # Native commands may have partially executed. Never rerun them or
        # locally destroy an object already owned by the server.
        req['result'] = {'request_id': token, 'status': 'finalize_failed',
                         'error': str(exc), 'owner_uid': unit.uid, 'ugc_id': comp._ugc_id,
                         'scope': 'server', 'persistence_verified': False,
                         'note': '可能已部分确认；已停止自动重试，保留现场供检查。'}
    return _reply(req)


# ---------------------------------------------------------------------------
# 批量摆放（2026-10-02 提速）：一条命令登记 N 件 / 一条批量 poll 轮询整批。
# 原型实测（probe/_batch_place_bench.md，试验场 2026.9.16 实机）：
#   登记 0.001~0.003ms/件；N=1000 全部落定 ≤0.7s；finalize ≈2.1ms/件；
#   0 丢失；按 uid 序与发送序严格 FIFO。
#   ⇒ 「引擎单帧建件预算」远大于批量大小（1000 件在一个采样窗口内到齐），
#     瓶颈从来是单笔在途锁的往返排队，不是引擎建件。
# ---------------------------------------------------------------------------

# 控件池的键：控件全图共用一个单元、unit.ugc_type 不可信，只能按组件类型聚合
_WIDGET_KEY = '__ui_widget__'


def _compact(req):
    """批量回执的单条紧凑摘要：逐 token 的 status + 建映射所需的最少字段。

    接受两种输入：请求账本（{'id','spec','result',...}）或其回执（`_reply` /
    `_confirm_*` 的返回 dict）——批量认领直接拿到 `_confirm_*` 的返回，两种
    都要能压成同一形状。
    """
    if isinstance(req, dict) and 'id' in req and 'spec' in req:
        r = req.get('result')
        if r is not None:
            out = {}
            for k in ('request_id', 'status', 'ugc_id', 'owner_uid', 'item_id',
                      'group_ugc_ids', 'error'):
                if r.get(k) is not None:
                    out[k] = r.get(k)
            # 越界缩放线索与单笔路径一致：placed 也要显式带回来（见 _reply）
            dev = (req.get('spec') or {}).get('scale_out_of_catalog_range') or []
            if dev and out.get('status') == 'placed':
                out['catalog_scale_deviation'] = dev
            return out
        out = {'request_id': req['id'], 'status': req.get('status')}
        if req.get('note'):
            out['note'] = str(req['note'])[:160]
        return out
    out = {}
    for k in ('request_id', 'status', 'ugc_id', 'owner_uid', 'item_id',
              'group_ugc_ids', 'error', 'catalog_scale_deviation'):
        if isinstance(req, dict) and req.get(k) is not None:
            out[k] = req.get(k)
    return out


def _receipt_alive(hit, cm):
    """幂等回执指向的组件**还在当前图上**吗？（2026-10-02 defect D：跨 run 假回执）

    账本 `requests` 与留痕表 `settled` 都是 room 级的 —— 同一个 room 会话里换一份
    progress 文件重跑时，由「快照内容派生的 run id + 位次」构成的 token 会命中**旧 run
    留下的** placed 回执；如果照单全收，调用方会把它当"这件摆好了"，而图上根本没有它
    （实机：i=0..99 全是这样，100 件静默丢失，进度里还留着 13 个过期的 ugc_id）。

    回读判据刻意保守：
      * 还没定论的请求（没有 result）→ True：可能正在飞，**绝不重发**（既有幂等语义）；
      * 失败定性（finalize_failed / ambiguous / conflict / overdue）→ True：出路是检查现场，
        不是重发；
      * 只有 placed 回执才回读组件表；成组创建的成员 id 任一在场即算活着。
    """
    r = (hit or {}).get('result')
    if not isinstance(r, dict):
        return True
    if r.get('status') != 'placed':
        return True
    gids = list(r.get('group_ugc_ids') or [])
    if r.get('ugc_id') is not None:
        gids.append(r['ugc_id'])
    if not gids:
        # placed 却没有 id（控件类可能只回 ugc_ids）：用 ugc_ids 兜底，仍取不到就当活着
        gids = list(r.get('ugc_ids') or [])
    if not gids:
        return True
    return any(g in cm._ugc_id2component for g in gids)


def begin_batch(scene, edit, args, planned, room, cm, ui):
    """批量登记：在**同一次主线程回调**里逐件调引擎 `_add_unit_server`。

    `planned` 与 args['items'] 一一对应（scene 层已逐件 plan；plan 失败的位次放
    {'plan_error': 原因} 或 None，按「试了失败」逐件定性，不拖垮整批）。
    护栏映射（HANDOVER_RESTORE_SPEED_20261002 §3）：
      1. MAX_REQUESTS：批量大小 + 在途未定论数 ≤ 128（重试 token 不重复占额）。
      2. 同规格去重：token 未给时按 spec 等值查未定论/本批请求，命中即幂等返回，
         绝不建两件；token 已在账的走与单笔路径同一套幂等回执。
      3. 只有 placed 释放额度：finalize_failed 等失败定性留在账上占额（见 _prune）；
         存在**不属于本批 token** 的未定论请求 ⇒ 整批拒绝，先检查现场。
      4. 成组创建（204041）仍是一个请求：spec 自带 group_transforms，登记不拆分。
    批量登记**不设** active 单指针（per-token 账本）；「别在未定论上叠新件」由
    「不属于本批的未定论请求存在即拒」这条等价承担。登记是纯发送（实测
    0.003ms/件）且发生在主线程回调里——期间不可能有实体落地（建件异步、回调
    阻塞帧循环），本批所有请求的 before 快照必然一致。
    """
    state = _state(room)
    _prune(state)
    items = args.get('items') or []
    if not isinstance(items, list) or not items or len(items) != len(planned):
        raise ValueError('items 必须是与 plan 结果等长的非空数组')
    tokens = []
    for it in items:
        tok = (it or {}).get('request_id')
        if tok is not None and (not isinstance(tok, str) or not 1 <= len(tok) <= 128):
            raise ValueError('request_id 必须为 1 至 128 字符的字符串')
        tokens.append(tok)
    for i, tok in enumerate(tokens):
        if tok is not None and tok in tokens[i + 1:]:
            raise ValueError('同一批内 request_id 重复: %s' % tok)
    incoming = {t for t in tokens if t is not None}
    blockers = [t for t, r in state['requests'].items()
                if not _settled_ok(r) and t not in incoming]
    if blockers:
        raise ValueError(
            '存在 %d 条不属于本批的未定论摆放请求（最早 request_id=%s）；'
            '请先 place_poll_batch 查询/重试它们，再开新批'
            % (len(blockers), blockers[0]))
    n_new = sum(1 for spec, token in zip(planned, tokens)
                if spec and 'plan_error' not in spec
                and (token is None or (token not in state['requests']
                                       and token not in state['settled'])))
    unsettled_n = sum(1 for r in state['requests'].values() if not _settled_ok(r))
    if unsettled_n + n_new > MAX_REQUESTS:
        raise ValueError(
            '批量大小 %d + 在途未定论 %d 超过 %d 上限；请调小批量'
            % (n_new, unsettled_n, MAX_REQUESTS))
    if ui.ugc_params.get('is_dragging_create'):
        raise ValueError('编辑器正在拖拽创建，请结束当前操作后再调用')
    out = []
    registered = 0
    for idx, (it, spec, token) in enumerate(zip(items, planned, tokens)):
        row = {'index': idx, 'request_id': token}
        if not spec or 'plan_error' in spec:
            row.update({'status': 'error',
                        'error': str((spec or {}).get('plan_error')
                                     or 'plan 失败')[:200]})
            out.append(row)
            continue
        # 幂等：token 已在账（未定论/已定论留痕）且 spec 相同 → 原样返回，绝不重发。
        # ⛔ 但"已定论且 placed"的回执必须**回读核实组件还在图上**：账本与留痕表是
        # room 级的（跨 progress 文件、跨 run 存活），而 token 由快照内容派生的 run id +
        # 位次构成 ⇒ 在同一个 room 会话里换一份 progress 重跑，位次相同的请求会命中
        # **旧 run 的留痕**、拿到一条 placed 回执却一件都不建（2026-10-02 实机：i=0..99
        # 全部如此，进度里写着 placed、图上只剩 13 个过期的 ugc_id ⇒ 交付少 100 件）。
        prev = state['requests'].get(token) if token else None
        s_prev = state['settled'].get(token) if token else None
        if prev is not None or s_prev is not None:
            hit = prev or s_prev
            if hit['spec'] != spec:
                row.update({'status': 'error',
                            'error': '同一 request_id 不可用于不同的摆放参数'})
                out.append(row)
                continue
            if not _receipt_alive(hit, cm):
                # 回执指向的组件已经不在了 ⇒ 这是旧 run 的残影，不是幂等命中：
                # 摘掉它，按新请求重新登记（下面走正常登记分支）。
                state['requests'].pop(token, None)
                state['settled'].pop(token, None)
            else:
                row['request_id'] = hit['id']
                row.update(_compact(hit))
                out.append(row)
                continue
        if token is None:
            # 护栏 2：批量内同规格去重（未给 token 的请求共用同一件）
            twin = None
            for r0 in state['requests'].values():
                if not _settled_ok(r0) and r0['spec'] == spec:
                    twin = r0
                    break
            if twin is not None:
                row['request_id'] = twin['id']
                row.update(_compact(twin))
                out.append(row)
                continue
        if spec['component_type'] not in scene.SERVER_COMPONENT_TYPES:
            row.update({'status': 'error', 'error':
                        '组件类型 %s 未列入服务端摆放白名单（共 %d 类，见 '
                        'fuxiang_scene.SERVER_COMPONENT_TYPES）'
                        % (spec['component_type'],
                           len(scene.SERVER_COMPONENT_TYPES))})
            out.append(row)
            continue
        token = token or uuid.uuid4().hex
        req = {'id': token, 'status': 'pending', 'started': time.monotonic(),
               'spec': spec, 'ui': ui, 'cm': cm,
               'before_uids': {getattr(c, '_owner_uid', None)
                               for c in cm._ugc_id2component.values()},
               'before_ids': {_as_int(k) for k in cm._ugc_id2component}}
        state['requests'][token] = req
        is_role_begin = spec['component_type'] in scene.ROLE_COMPONENT_TYPES
        try:
            if is_role_begin:
                # 角色只有在创建那一步携带坐标才会把落位持久化（EDIT_OPS.md §11.1）
                ui._add_unit_server([spec['item_id']], pos=tuple(spec['position']))
            else:
                ui._add_unit_server([spec['item_id']])
        except Exception as exc:
            req['status'] = 'send_unknown'
            req['note'] = '发送异常，结果未知；保留请求以避免重复创建: %s' % exc
        registered += 1
        row['request_id'] = token
        row.update(_compact(req))
        out.append(row)
    return {'results': out, 'registered': registered, 'poll_after_ms': 0,
            'note': ('批量已登记 %d 件（引擎侧异步建件，实测 ≤1s 落定）；'
                     '请用 place_poll_batch 轮询整批。等待期间请勿拖入新物件。'
                     % registered)}


def _batch_pool(base, ui, cm, room):
    """把「基线快照之后新出现」的组件按 item_id 分池，供批量认领。

    返回 {item_id: [entry, ...]}，entry=(kind, order, unit, comp)：
      entity  新实体（普通类，或**处于待确认态**的逻辑单元）——单件/成组都从这里出
      global  挂在共享单元上的新组件（运动器/剧情等，位置不在单元上）
      widget  界面控件组件（200000，全图共用一个单元，入 _WIDGET_KEY 池）
    order 是**认领键**（实体用 uid，global/widget 用组件自己的 ugc_id）；实测引擎按
    发送序分配 uid / ugc_id（FIFO，bench B 轮交错实测 0 失序），按 order 升序认领
    即按请求序配对。已在编辑器登记集合里的（手动放置/已确认）不进池——批量永不认领。

    ⚠️ 2026-10-02 首次实机全量（1.5 万件）第 1 批就暴露的这个函数的两处缺陷，
    根因都在**分类/认领键**上而不是在 finalize 里：

    1. **逻辑单元上的实体同样要求变换**（核心实锤）：item 301001（组件 100001）落地后
       owner 是 `UgcLogicUnit`，但它同样进了 `del_unit_uids['unit']`；单笔路径据
       「待确认态」把它交给 `_finalize_unit`（施加变换 + 严格回读），而这里按类名
       一律判 'global' ⇒ 走 `_confirm_global_component`（**从不施加变换**）⇒ 回执
       `placed`、实况却停在隐藏哨兵 `[0,-65536,0]`（run-1 的 7 个方块，用户目视
       "位置不对"）。反证：单笔路径两次全量跑的回读只报 333 件不在请求坐标上
       （332 件 203911 运动器 + 1 件，位置真值都不在单元上），14318 件方块一件没报
       ⇒ 单笔路径确实走了 `_finalize_unit`。
       ⚠️ 实测 UgcGlobalUnit（运动器 203911 的宿主）**不**进 `del_unit_uids`
       ——它照旧走 global（位置真值在 component_info，由属性回写阶段写），
       这条判据不会把运动器误判成实体。
       ⛔ **判据只能是 `del_unit_uids['unit']`，不能带 `create_units`**（2026-10-02 实机
       连拍取证，`probe/live/probe_window.py`）：一批登记 7 件方块，落地后
       `room.del_unit_uids['unit']` 有全部 7 个 uid，而 `ui.ugc_params['create_units']`
       **只有最后一个**（7 件里 6 件 `cu=False`）。单笔路径之所以能用
       「del_bag **且** create_units」，是因为它一次只建 1 件、`create_units`
       恰好就是那一件；批量路径照抄这条判据 ⇒ 除最后一件外全部被误判成全局组件。
       （7 件里恰好只有第 7 件落对了位置 —— 这就是"错 6 件"的实机指纹。）
    2. **共享单元上的多件组件必须各自可认领**：49 件 203911 运动器全挂在**同一个**
       新建 UgcGlobalUnit（uid 1000077）上，旧代码把"新单元"的认领键写成 unit.uid ⇒
       49 条池记录的键全相同，`_claim` 的 claimed 集合每轮只放行 1 条 ⇒ 其余件在
       单次批量 poll 里**永远认领不到**（run-1：54 placed / 46 pending，9.6s 早停；
       每次 poll 恰好多放行 1 件，49 轮 × 150ms 退避正是那段耗时）。后果不只是慢：
       每轮反复确认的是同一个组件（49 行进度全是 `ugc_id=119`），另外 48 个组件
       始终留在 `del_unit_uids['component']` 里 —— 一保存就会被当**删除**上送。
       认领键改用组件自己的 ugc_id（= 创建序 = 发送序）⇒ 一轮全部认领，
       且 `_claim` 里「order ∈ before_ids」的跨批守卫语义不变。
       实机验收（`probe/live/live_defect_a.py`）：run-1 卡住的 46 个 token
       修复后**一轮全部 placed**（旧实现每轮只多认领 1 件），84 件待删组件降到 40。
    """
    base_uids = base['before_uids']
    base_ids = base['before_ids']
    del_bag = (room.del_unit_uids.get('unit', [])
               if hasattr(room, 'del_unit_uids') else [])
    registered = getattr(ui, 'all_ugc_uid_set', set()) or set()
    pools = {_WIDGET_KEY: []}
    for key, comp in cm._ugc_id2component.items():
        gid = _as_int(key)
        if gid is None or gid in base_ids:
            continue
        unit = getattr(comp, '_owner', None)
        if unit is None:
            continue
        uid = getattr(unit, 'uid', None)
        if uid is not None and uid in registered:
            continue
        if _component_type(comp) == UI_CUSTOM_COMPONENT_TYPE:
            pools[_WIDGET_KEY].append(('widget', gid, unit, comp))
            continue
        item_id = getattr(unit, 'ugc_type', None)
        unit_is_new = uid is not None and uid not in base_uids
        # 「待确认态」只看 `del_unit_uids['unit']`：批量登记时 create_units 只留最后一个
        # 新建单元（实测 7 件里 6 件 cu=False），带上它就会把方块误判成全局组件。
        in_pending = uid is not None and uid in del_bag
        if unit_is_new and in_pending:
            # 服务端本次新建、仍挂在待确认袋里 ⇒ 未确认实体：必须走 _finalize_unit
            # （施加变换 + 回读校验）。普通类与 UgcLogicUnit 都从这里出。
            pools.setdefault(item_id, []).append(('entity', uid, unit, comp))
        elif unit_is_new and type(unit).__name__ in ('UgcGlobalUnit',
                                                     'UgcLogicUnit'):
            # 新单元却不在待确认袋：全局单元上的组件（位置真值在 component_info，
            # 由属性回写阶段写）—— 走 global。
            pools.setdefault(item_id, []).append(('global', gid, unit, comp))
        elif unit_is_new:
            # 新实体不在待确认袋：不符合认领条件（与单笔路径 conflict 同因），不认领。
            continue
        else:
            # 挂在**旧**单元上的新组件（运动器/剧情…）：单元不是本批建的，走 global。
            pools.setdefault(item_id, []).append(('global', gid, unit, comp))
    for lst in pools.values():
        lst.sort(key=lambda e: e[1])
    return pools


def _claim(scene, pools, req, claimed):
    """为一个请求从池里认领实体/组件。返回 (entry_or_list, reason_or_None)。

    认领即标记（claimed），绝不让两个请求共享同一件；还要求候选**不在该请求
    自己的 before 快照里**（跨批续做时账里混着不同基线的请求，防止把登记前
    就有的件认领走）。成组类型一次要满 group_size 个 entity。
    """
    spec = req['spec']
    item_id = spec['item_id']
    group_size = scene.GROUP_CREATE_TYPES.get(spec.get('component_type'))
    pool = (pools.get(item_id) or []) + (pools.get(_WIDGET_KEY) or [])
    if not pool:
        return None, '池中暂无该类型的候选'
    if _is_ui_widget(req):
        take = [e for e in pool if e[0] == 'widget' and e[1] not in claimed
                and e[1] not in (req.get('before_ids') or set())][:1]
        if not take:
            return None, '池中暂无未认领的控件组件'
        claimed.add(take[0][1])
        return take[0], None
    if group_size:
        take = [e for e in pool if e[0] == 'entity' and e[1] not in claimed
                and e[2].uid not in req['before_uids']][:group_size]
        if len(take) < group_size:
            return None, '成组类型需 %d 个实体，池中不足' % group_size
        for e in take:
            claimed.add(e[1])
        return take, None
    for e in pool:
        if e[1] in claimed:
            continue
        if e[0] == 'entity' and e[2].uid in req['before_uids']:
            continue
        if e[0] == 'global' and e[2].uid in req['before_uids'] \
                and e[1] in (req.get('before_ids') or set()):
            continue
        claimed.add(e[1])
        return e, None
    return None, '池中暂无未认领的候选'


def poll_batch(scene, edit, args):
    """批量 poll：一条命令推进整批请求（护栏 3/6 的批量形态）。

    逐 token 推进 pending 请求；已定论的原样回执（幂等）。池化认领：同一次
    主线程回调里登记的本批请求共享 before 快照，按「item_id 池 + order 升序
    FIFO」把新实体/组件配给同类型请求（配对依据 = bench B 轮实测的引擎 FIFO），
    成组类型一次认领满 group_size 个。回执逐 token 紧凑（_compact）；
    还有 pending 时建议 poll_after_ms=150，全部定论时为 0。
    """
    room, cm = scene.context(edit)
    state = _state(room)
    tokens = args.get('request_ids')
    if not isinstance(tokens, list) or not tokens \
            or not all(isinstance(t, str) for t in tokens):
        raise ValueError('request_ids 必须是非空字符串数组')
    if len(tokens) > MAX_REQUESTS:
        raise ValueError('一次最多轮询 %d 个请求' % MAX_REQUESTS)
    ui = scene.get_edit_ui(edit)
    if ui.ugc_params.get('is_dragging_create'):
        raise ValueError('编辑器正在拖拽创建，无法安全认领候选；请结束后再轮询')
    results = []
    pending = []
    for token in tokens:
        req = state['requests'].get(token)
        if req is None:
            settled = state['settled'].get(token)
            if settled is not None:
                results.append(dict(_compact(settled), request_id=token))
            else:
                results.append({'request_id': token, 'status': 'error',
                                'error': '当前地图会话没有此 request_id'})
            continue
        if req.get('result') is not None:
            results.append(_compact(req))
            continue
        req['polled'] = True
        if req['status'] in ('ambiguous', 'conflict') and not (
                req['status'] == 'ambiguous'
                and scene.GROUP_CREATE_TYPES.get(
                    (req.get('spec') or {}).get('component_type'))):
            results.append(_compact(req))
            continue
        if ui is not req['ui'] or cm is not req['cm']:
            raise ValueError('编辑器上下文已更换，未继续操作旧请求')
        if req['status'] == 'ambiguous':
            req['status'] = 'pending'
        pending.append((token, req))
    if pending:
        pools = _batch_pool(pending[0][1], ui, cm, room)
        claimed = set()
        for token, req in pending:
            got, why = _claim(scene, pools, req, claimed)
            if got is None:
                if time.monotonic() - req['started'] >= 180:
                    req['status'] = 'overdue'
                    req['note'] = ('已等待 180 秒仍未在池中认领到候选（%s）；'
                                   '可继续查询，不会自动重发。' % (why or ''))[:160]
                results.append(_compact(req))
                continue
            entries = got if isinstance(got, list) else [got]
            kind = entries[0][0]
            if kind == 'widget':
                _k, _o, unit, comp = entries[0]
                results.append(_compact(_confirm_ui_component(
                    scene, req, state, ui, [(comp._ugc_id, comp, unit)])))
            elif kind == 'global':
                _k, _o, unit, comp = entries[0]
                results.append(_compact(_confirm_global_component(
                    scene, req, state, ui, (unit, comp))))
            else:
                results.append(_advance_entity(
                    scene, req, state, ui, room, token,
                    [(unit, comp) for _k, _o, unit, comp in entries]))
    n_placed = sum(1 for r in results if r.get('status') == 'placed')
    unsettled = sum(1 for r in results if r.get('status') in
                    ('pending', 'finalizing', 'ambiguous'))
    return {'results': results, 'n': len(results), 'placed': n_placed,
            'unsettled': unsettled,
            'poll_after_ms': 0 if unsettled == 0 else 150,
            'note': ('批量轮询：placed %d / 仍 pending %d。全部定论后即可保存'
                     '或开新批。' % (n_placed, unsettled))}


def _advance_entity(scene, req, state, ui, room, token, candidates):
    """批量认领后的实体确认：与 _poll_pending 的尾部同一条路径（含成组）。"""
    group_size = scene.GROUP_CREATE_TYPES.get(req['spec'].get('component_type'))
    if group_size and len(candidates) >= group_size:
        return _compact(_confirm_group_create(scene, req, state, ui, room, token,
                                              candidates, group_size))
    if len(candidates) != 1:
        req['status'] = 'ambiguous'
        req['note'] = '发现多个同类新物件，无法对应请求；未移动或确认任何一个。'
        return _compact(req)
    unit, comp = candidates[0]
    if (unit.uid in ui.all_ugc_uid_set
            or ui.ugc_params.get('is_dragging_create')):
        req['status'] = 'conflict'
        req['note'] = '候选物件不在独占的待确认状态；未改动。请检查是否同时进行了手动放置。'
        return _compact(req)
    req['status'] = 'finalizing'
    is_role = req['spec'].get('component_type') in scene.ROLE_COMPONENT_TYPES
    try:
        actual = _finalize_unit(scene, ui, room, unit, comp, req['spec'], token,
                                is_role)
        req['result'] = {'request_id': token, 'status': 'placed', 'scope': 'server',
                         'ugc_id': comp._ugc_id, 'owner_uid': unit.uid,
                         'item_id': req['spec']['item_id'], 'transform': actual,
                         'editor_registered': True, 'persistence_verified': False,
                         'catalog_hidden': req['spec'].get('catalog_hidden', False),
                         'object_list_eligible': req['spec'].get('object_list_eligible',
                                                                 True),
                         'component_count': ui.get_all_ugc_component_nums(),
                         'occupancy': ui.get_current_occupancy(),
                         'note': '已完成原生确认；持久化须保存并重进地图核实。'}
        state['active'] = None
    except Exception as exc:
        req['result'] = {'request_id': token, 'status': 'finalize_failed',
                         'error': str(exc), 'owner_uid': unit.uid,
                         'ugc_id': comp._ugc_id,
                         'scope': 'server', 'persistence_verified': False,
                         'note': '可能已部分确认；已停止自动重试，保留现场供检查。'}
    return _compact(req)
