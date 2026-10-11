"""Scene editing: asynchronous server placement or explicit local preview."""
import math

# 唯一经过完整实机验证（含视觉验收）的组件类型：普通场景障碍物
VERIFIED_COMPONENT_TYPE = 100000
# Distinct native contracts, even when catalog records share the same model.
# 2026-09-18 实机修正：该白名单原先只列 100000/202014，过窄。
# 实测服务端同样受理 203003 REGION 与 203004 MIST 的 add_unit（place_item mode=server
# 一次即 placed：ugc_id 119/120，位置 [-430,51,200]/[-430,53,205] 均正确）。
# 结论：服务端并非按 component_type 白名单拒绝；此前限制只是客户端的保守策略。
# 2026-09-18 P3-1 追加 202041 人数检测（实测 _add_unit_server 受理、place_poll 一次 placed）。
# 2026-09-19 大幅扩容（builds/verify_unusable/ 复核）：type_sweep 的 45 个 placed 类型
# + 原「finalize_failed 误判不可用」的 23 个类型（根因是 _apply_full_transform 强读
#   ugc_size，已改容错）+ 202006/202007/203020 —— 全部实机 placed。
# 未加入的（会 pending/ambiguous，标准路径不适用）：
#   203001 龙卷风 / 201003 大门  —— 一次创建 2 个实体，须整组 _normal_create_units
#   10001001/10002001/10004001/10006001/10009001 全局单例 —— 可摆但副本删不掉，故意不放行
# 2026-09-19 追加「无独立实体」的 7 类：203911/203930/203920/203921/10007001/200000/200001。
#   它们服务端只创建组件并挂在 UgcGlobalUnit 上（不占占用值）；place_poll 已支持
#   按「新组件 ugc_id」匹配并登记保存变更集，故纳入标准路径（此前会一直 pending）。
# 2026-09-21 追加 10007002 DIALOGUE / 10007003 CHOICE（剧情编辑三件套的另两件）：
#   同族无实体逻辑组件，实测 place_item(mode=server) 一次即 placed
#   （ugc_id 817/818，owner UgcGlobalUnit 1000777/1000778，editor_registered=true）。
SERVER_COMPONENT_TYPES = frozenset({
    100000, 100001, 100002, 100003, 100004, 100005, 100006, 100007, 100008,
    101001, 101002, 102001, 102002, 103001,
    201001, 201002, 201004, 201008, 201010,
    202001, 202002, 202003, 202004, 202005, 202006, 202007, 202011, 202012,
    202014, 202015, 202016, 202018, 202019, 202022, 202023, 202024, 202025,
    202026, 202027, 202028, 202029, 202030, 202032, 202041, 202042, 202051,
    203003, 203004, 203005, 203006, 203012, 203016, 203018, 203019, 203021,
    203022, 203025, 203026, 203027, 203028, 203900, 203911, 203912, 203920, 203921, 203930,
    10007001, 10007002, 10007003, 200000, 200001,
    204001, 204002, 204005, 204041, 205029, 206001, 206002,
    207001, 207002, 207003, 207004,
})

# 角色类（求生者 / 监管者）：服务端受理，但**只在创建那一步携带坐标**才落库
# （EDIT_OPS.md §11.1 实测：`_add_unit_server` 有个可选 `pos` 参数会随
# `add_unit` 一起上送；创建后再 set_position 的位置会被服务端拉回哨兵位）。
# 摆放状态机因此对这两类走专用分支：begin 带 pos、确认时只改位置
# （`MyCivilianUnit` 没有 ugc_size/ugc_dir，套 `_apply_full_transform` 会炸）、
# 确认后 `hide_collision_pos(False)` 取消半隐藏。
ROLE_COMPONENT_TYPES = frozenset({101001, 102001})

# 一次 `add_unit` 会生成**多个实体**的类型：{component_type: 实体数}。
# 实机 2026-10-01（春岚）：item 204041「推荐检查点」一次产出 2 个 UgcLogicUnit，
# 并且引擎自动把两件的 `component_info['group_components']` 写成**互指**
# —— 成组关系由创建本身建立，不需要（也没法从 Python 侧）人工框选。
# 摆放状态机因此要对这类"整组认领"：候选数≠1 时不能一律判 ambiguous，
# 否则这件永远摆不出来（实测就是 ambiguous）。
GROUP_CREATE_TYPES = {204041: 2}

# ---------------------------------------------------------------------------
# ⚠️ 与下方 VERIFIED_TYPES 的关系（2026-09-22 补说明）
#
# 两套阀门职责不同，**不要以为是同一份白名单**：
#   * SERVER_COMPONENT_TYPES —— place_item(mode=server) 的**准入**阀门（本集合）
#   * VERIFIED_TYPES         —— plan() 的**准入**阀门（本地构造全量实测 90 种）
#
# plan() 返回值里 `server_place_supported` / `server_begin_allowed` 用的是**本集合**，
# 所以二者的差集不会静默出错，但仍会出现「plan 通过、place 被拒」：
#   * VERIFIED - SERVER（13 种）：本地构造已实测，服务端阀门未开
#     → plan 返回 server_begin_allowed=false + block_reason；
#       确要摆可在 fuxiang_eval 内临时扩展本集合，或改 mode=local_preview。
#   * SERVER - VERIFIED（2 种：202006 / 202007）：
#     → plan 需显式 allow_unverified=true 才过，但 begin 直接放行。
#
# 差集随代码变动，查看实时值用：python cli/consts.py
#
# ⚠️ 2026-09-22 结论修正：**两套阀门不等价是设计，不是遗漏**。
# 差集里这 13 个类型各有明确不放行的原因，见下表（此前只散落在注释里，
# 容易被误读成"漏加"，故改为结构化数据并对外可查）。
# ---------------------------------------------------------------------------

# VERIFIED_TYPES 有、但 SERVER_COMPONENT_TYPES 故意不含的类型的**原因**
# （键 = component_type；值 = 为什么不走标准 server 摆放路径）
SERVER_EXCLUDED_REASONS = {
    # —— 一次创建多个实体：标准路径只认领 1 个新 ugc_id，会 pending/ambiguous ——
    201003: "DOOR 大门：一次创建 2 个实体（门扇+门框），须整组 _normal_create_units",
    203001: "TORNADO 龙卷风：一次创建 2 个实体，同上",
    # —— 服务端不受理 ——
    203013: "PHOTO_WALL 图片墙：实测服务端唯一真不受理的类型",
    # —— 全局单例：能摆但副本删不掉，故意不放行 ——
    10001001: "GLOBAL 全局",
    10002001: "VICTORY 胜利条件",
    10003001: "FOG 雾",
    10003002: "LUT 调色",
    10004001: "INSTALL 机关",
    10006001: "GAME_TIME 游戏时间",
    10008001: "COUNTDOWN 倒计时",
    10009001: "SKYBOX 天空盒",
    # —— 地图自带 / 语义特殊 ——
    201009: "ANCHOR 边界锚点：地图自带，不作为可摆物件（preview 的 BOUNDARY_COMPONENT_TYPES）",
    103002: "NPC_ACTION_EDITOR：NPC 动作编辑器，实体由 NPC 侧生成，未查明可摆性，暂不放行",
}

# ---------------------------------------------------------------------------
# 全量实测白名单（2026-09-16 那轮，2026-09-30 / 10-01 两次复审追加）
#
# 对物件目录中出现的全部 104 种 component_type 逐个做「创建 → 校验 → 清理」
# 实测，104 次操作后组件索引与待保存队列均完全复原。那一轮的结果是
#   90 种可摆放、14 种不可摆放。⚠️ 但那轮走的是**客户端本地构造**路径，
#   其中两类被判"不可摆"其实是本地路径的问题、不是服务端拒绝：
#     · 101001/102001 角色类 —— 服务端受理，且只有"创建即带 pos"才落库（EDIT_OPS.md §11）。
#       2026-09-30 移出 UNSUPPORTED，摆进去走 ROLE_COMPONENT_TYPES 专用分支；
#       本轮只在春岚本图验证到 placed+本地回读（重进证据来自 §11 的原生序列），
#       故**暂不入白名单**，还原要显式 `allow_unverified`（restore 侧 `--include-not-placeable`）。
#     · 204041 推荐检查点 —— 当年 "failed to create model!" 是目录 model_path 指向 .pse。
#       2026-10-01 走服务端路径摆→保存→**重进**逐位回读一致 ⇒ 已入白名单（见表尾）。
# 列入白名单的类型默认放行；白名单外仍需显式 allow_unverified=true。
# ---------------------------------------------------------------------------
VERIFIED_TYPES = frozenset({
    100000, 100001, 100002, 100003, 100004, 100005,           # OBSTACLE, GEOMETRY, AIRWALL, TARES, CROW, CARRIER
    100006, 100007, 100008, 101002, 102002, 103001,           # CHINATOWN_DOLL, OBSTACLE_SCALE, MODIFIER_EFFECT, NPC_CIVILIAN, NPC_BUTCHER, GHOUL
    103002, 200000, 200001, 201001, 201002, 201003,           # NPC_ACTION_EDITOR, UI_CUSTOM, UI_NATIVE, GENERATOR, HOOK, DOOR
    201004, 201008, 201009, 201010, 202001, 202002,           # CAVE, ESCAPE_REGION, ANCHOR, END_REGION, PANEL, WINDOW
    202003, 202004, 202005, 202011, 202012, 202014,           # BOX, CUPBOARD, STALL, BROKEN_WINDOW, TRAMPOLINE, BASEMENT
    202015, 202016, 202018, 202019, 202022, 202023,           # ROTATE_WALL, ELEVATE_WALL, CATAPULT, HANDHELD_POINT, STAIRS, RACE_TRAMCAR
    202024, 202025, 202026, 202027, 202028, 202029,           # FIXED_STATUE, RANDOM_STATUE, RANDOM_TENTACLE, BELL_AREA, VIOLIN_AREA, FOG_HIT
    202030, 202032, 202041, 202042, 202051, 203001,           # SPEED_UP_AREA, SMALL_AREA, DETECT_REGION, FORBID_REGION, SHOWCASE, TORNADO
    203003, 203004, 203005, 203006, 203012, 203013,           # REGION, MIST, CLOCK, COUNTER, LOGIC, PHOTO_WALL
    203016, 203018, 203019, 203021, 203022, 203025,           # DESTROY_REGION, CAMERA, VARIABLE_AIR_WALL, RANDOM_EVENT, INTER_AREA, CHAT_EVENT
    203026, 203027, 203028, 203900, 203911, 203912,           # GUIDER, CHARA_PIC_WALL, TAKE_PHOTO_WALL, COMPOSITE, MOTION, MOTION_POINT
    203920, 203921, 203930, 204001, 204002, 204005,           # MUSIC_EVENT, CAM_POS, COORDINATE_OFFSET_EDITOR, POSTER_WALL, SKETCH_PHOTO_WALL, CHECKPOINT_ROUTE
    205029, 206001, 206002, 207001, 207002, 207003,           # EMOJI_WALL, POINT_LIGHT, POINT_SHADOW, ELBOW_GUARD_RESTRICT_REGION, RUGBY_RESTRICT_REGION, ROCKET_RESTRICT_REGION
    207004, 10001001, 10002001, 10003001, 10003002, 10004001, # ARROW_BLOCK, GLOBAL, VICTORY, FOG, LUT, INSTALL
    10006001, 10007001, 10007002, 10007003, 10008001, 10009001,  # GAME_TIME, STORY, DIALOGUE, CHOICE, COUNTDOWN, SKYBOX
    # 2026-10-01 实机补入（春岚，走完整"摆→保存→重进→逐位回读"）：推荐检查点是
    # **成组创建**（一次 add_unit 出 2 件、引擎自动写 group_components 互指），
    # 两件各落快照坐标、旋转与属性逐项一致。判据是重进后的读数，不是本地回执。
    204041,                                                      # RECOMMEND_CHECK_POINT
})

# 实测无法通过本接口摆放的类型及原因
UNSUPPORTED_TYPES = {
    # ⚠️ 2026-09-30 复审，移出这三类（都曾是"本地构造路径"的失败，不是服务端拒绝）：
    #   101001 / 102001 角色类 —— 服务端 add_unit 受理，且 EDIT_OPS.md §11 已用
    #     「创建即带 pos」多轮重进实证位置+属性+事件全落库。改走 ROLE_COMPONENT_TYPES
    #     专用分支，不再硬拒；仍未进 VERIFIED_TYPES（摆放状态机那条分支还没实机跑过）。
    #   204041 推荐检查点 —— 当年报 "failed to create model!" 的原因是目录的
    #     `model_path` 指向 `.pse` 特效文件（`probe/unusable_rootcause.md:42`），
    #     属客户端本地构造路径；服务端路径从未试过。
    #   两类现在都是 place_support="unknown"，需要显式 allow_unverified=true 才放行。
    201005: "不在组件类型注册表中",
    201006: "不在组件类型注册表中",
    202020: "不在组件类型注册表中",
    203008: "UI 组件依赖未就绪（setFontName）",
    203014: "UI 组件依赖未就绪（setFontName）",
    203015: "UI 组件依赖未就绪",
    203910: "不在组件类型注册表中",
    10012001: "不在组件类型注册表中",
    10012002: "不在组件类型注册表中",
    10012003: "不在组件类型注册表中",
    10012099: "不在组件类型注册表中",
}


def vector3(value, name, positive=False):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError('%s 必须是三个有限数值 [x,y,z]' % name)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
           not math.isfinite(v) or (positive and v <= 0) for v in value):
        raise ValueError('%s 包含无效数值' % name)
    return [float(v) for v in value]


def scale3(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value] * 3
    return vector3(value, 'scale', positive=True)


def context(edit):
    room = edit.get_room()
    if room is None or type(room).__name__ != 'WorldBattleUGCEdit':
        raise ValueError('当前不在随笔浮想编辑器（room 为空或已切走场景）。'
                         '若地图被重开过，本次摆放账本随 room 一起失效：旧 request_id '
                         '再 place_poll 会报「没有此 request_id」，不能据此判断成败。'
                         '恢复顺序：fuxiang_status 确认 room 已回来 → 用 edit_targets 的'
                         'position 与清单落点比对，只补缺失件（换新 request_id）→ 立即 '
                         'save_map 并核对 dropped_count。')
    cm = getattr(room, 'ugc_component_manager', None)
    if cm is None:
        raise ValueError('当前编辑器组件管理器不可用')
    return room, cm


def visible_model_index(data):
    out = {}
    for key, item in data.items():
        model = item.get('model_path')
        if model and not item.get('is_visible'):
            out.setdefault(model, []).append(int(key))
    return {k: sorted(v) for k, v in out.items()}


def catalog_identity(data, item_id, item, visible_models=None):
    """Catalog eligibility is not proof that a live row appears in a UI view."""
    model = item.get('model_path')
    if visible_models is None:
        visible_models = visible_model_index(data)
    return {'catalog_hidden': bool(item.get('is_visible')),
            'object_list_eligible': not bool(item.get('is_visible')),
            'same_model_visible_item_ids': [k for k in visible_models.get(model, [])
                                            if str(k) != str(item_id)]}


def _group_transforms(args, component_type, pos, rot, scale):
    """`GROUP_CREATE_TYPES` 的第 2..N 个成员各自的变换。

    缺省（没给全）就沿用主件变换 —— 两件叠在同一处，玩家再拖其中一件，
    这比拒绝请求更接近编辑器里"拖一次出一对"的实际行为。
    """
    n = GROUP_CREATE_TYPES.get(component_type)
    if not n or n < 2:
        return []
    raw = args.get('group_transforms') or []
    if not isinstance(raw, (list, tuple)):
        raise ValueError('group_transforms 必须是列表，每项形如 {position, rotation, scale}')
    if len(raw) > n - 1:
        raise ValueError('组件类型 %s 一次生成 %d 个实体，group_transforms 最多给 %d 项'
                         % (component_type, n, n - 1))
    out = []
    for entry in raw:
        entry = entry or {}
        out.append({'position': vector3(entry.get('position', pos), 'position'),
                    'rotation': vector3(entry.get('rotation', rot), 'rotation'),
                    'scale': scale3(entry.get('scale', scale))})
    while len(out) < n - 1:
        out.append({'position': list(pos), 'rotation': list(rot), 'scale': list(scale)})
    return out


def plan(edit, args):
    room, cm = context(edit)
    raw_id = args.get('item_id')
    if isinstance(raw_id, bool) or not str(raw_id).isdigit():
        raise ValueError('item_id 必须为正整数或数字字符串')
    item_id = int(raw_id)
    data = edit.get_item_data() or {}
    item = data.get(item_id) or data.get(str(item_id))
    if item is None:
        raise ValueError('未找到物件 item_id=%s' % item_id)
    component_type = int(item.get('component_id', 0))
    # 实测已知不可摆放的类型：直接给出原因，避免无谓尝试
    if component_type in UNSUPPORTED_TYPES:
        raise ValueError(
            '物件 %s 的组件类型 %s 实测无法通过本接口摆放：%s'
            % (item_id, component_type, UNSUPPORTED_TYPES[component_type]))
    # 白名单内的 90 种类型已通过全量实测，默认放行；其余需显式请求
    if component_type not in VERIFIED_TYPES and not args.get('allow_unverified'):
        raise ValueError(
            '物件 %s 的组件类型为 %s，不在已验证白名单中。'
            '确要尝试请显式传 allow_unverified=true。' % (item_id, component_type))
    # disable_edit 是编辑器 UI 层的模式限制（如 (1,3) 表示模式 1/3 下不可编辑）。
    # 实测已证明本接口不受其影响，故仅对「白名单外 + 未显式请求」的组合拦截。
    if (item.get('disable_edit') and component_type not in VERIFIED_TYPES
            and not args.get('allow_unverified')):
        raise ValueError(
            '物件目录标记为在部分编辑模式下不可编辑（disable_edit=%s），'
            '且其组件类型不在已验证白名单中；确要尝试请显式传 allow_unverified=true。'
            % (item['disable_edit'],))
    if args.get('model_path') is not None or args.get('unit_type') is not None:
        raise ValueError('本地摆放使用目录模型和 UGC_COM 实体类型，不支持覆盖')
    pos = vector3(args.get('position', [0, 0, 0]), 'position')
    rot = vector3(args.get('rotation', [0, 0, 0]), 'rotation')
    scale = scale3(args.get('scale', 1))
    limits = item.get('zoom_limit')
    scale_out_of_range = []
    if isinstance(limits, (tuple, list)) and len(limits) == 2:
        scale_out_of_range = [v for v in scale if v < limits[0] or v > limits[1]]
        if scale_out_of_range and not args.get('allow_out_of_range_scale'):
            # 这条是**编辑器滑块**的范围，不是引擎的硬限制。快照还原抄的是既有件
            # 的真实缩放，会被它整批拦下（2026-10-01 实机：春岚 214 个组合件的
            # z 轴都是 0.02，目录下限 0.1 ⇒ 全部报 "scale 超出物件缩放范围"，
            # 而几何件 301001 因为自己下限宽松反而能过 —— 组合层会整层摆不出来）。
            raise ValueError(
                'scale 超出物件缩放范围 %s（越界值 %s）。快照还原请显式传 '
                'allow_out_of_range_scale=true；回读仍严格按请求值校验。'
                % (limits, scale_out_of_range))
    model = item.get('model_path')
    if not model:
        raise ValueError('物件没有模型路径')
    conf = room.create_unit_with_info.__globals__['UNIT_TYPE_CONF']
    # 2026-09-19：plan（用 VERIFIED_TYPES）与 begin（用 SERVER_COMPONENT_TYPES）
    # 是两套阀门，会出现「plan 通过但 begin 被拒」。这里把 begin 的判定直接
    # 暴露出来，调用方不必等到报错才知道，也不必再猜「服务端面子上受不受理」。
    begin_allowed = component_type in SERVER_COMPONENT_TYPES
    # 成对/成组创建的类型（GROUP_CREATE_TYPES）：调用方可以给第 2..N 个成员各自的变换。
    # 不给就沿用主件变换（两件叠在同一处，人工再拖），给了就逐件校验。
    group_transforms = _group_transforms(args, component_type, pos, rot, scale)
    return {'item_id': item_id, 'component_type': component_type,
            'unit_type': conf.UGC_COM, 'model_path': model,
            'position': pos, 'rotation': rot, 'scale': scale,
            'group_transforms': group_transforms,
            # 抄自快照的缩放落在编辑器滑块范围外时记下越界值（引擎侧照收，
            # 回执里要看得见，见 allow_out_of_range_scale 的注释）
            'scale_out_of_catalog_range': scale_out_of_range,
            'scope': 'client_local', 'persistence_verified': False,
            'server_place_supported': begin_allowed,
            'server_begin_allowed': begin_allowed,
            'server_begin_block_reason': (
                None if begin_allowed else
                '客户端阀门 SERVER_COMPONENT_TYPES 未含该类型；这不是服务端限制——'
                '服务端 add_unit 无类型白名单，可在 fuxiang_eval 内临时扩展该集合后摆放，'
                '或改用 mode=local_preview（不持久化）。'),
            **catalog_identity(data, item_id, item),
            'component_type_verified': component_type in VERIFIED_TYPES,
            'visually_verified': component_type == VERIFIED_COMPONENT_TYPE}


def next_component_id(cm):
    used = {int(k) for k in cm._ugc_id2component if str(k).isdigit()}
    for ids in cm._component_type2ugc_ids.values():
        used.update(int(k) for k in ids if str(k).isdigit())
    return max(used | {int(cm._ugc_id_counter), 0}) + 1


def next_unit_id(room, cm):
    used = [int(c._owner_uid) for c in cm._ugc_id2component.values()
            if str(getattr(c, '_owner_uid', None)).isdigit()]
    uid = max(used + [1000000]) + 1
    while room.unit_mgr.get_unit(uid) is not None:
        uid += 1
    return uid


def transform(unit):
    def xyz(v):
        return [float(v.x), float(v.y), float(v.z)]
    # 2026-09-19 实测：BoxUnit/PanelUnit/CrowUnit 等 21 个实体类只有变换 setter，
    # 没有 ugc_dir / ugc_size 读属性（读会 AttributeError）。缺读属性时返回 None，
    # 由 place_poll 跳过对应维度的回读校验（不影响可读实体的一致性校验）。
    rot = getattr(unit, 'ugc_dir', None)
    size = getattr(unit, 'ugc_size', None)
    return {'position': xyz(unit.get_position3()),
            'rotation': xyz(rot) if rot is not None else None,
            'scale': xyz(size) if size is not None else None}


def cleanup(room, cm, uid, ugc_id):
    """Remove only this newly allocated unit and its matching component."""
    errors = []
    comp = cm._ugc_id2component.get(ugc_id)
    if comp is not None and getattr(comp, '_owner_uid', None) != uid:
        return {'complete': False, 'errors': ['组件 owner 不匹配，未清理']}
    unit = room.unit_mgr.get_unit(uid)
    try:
        if comp is not None:
            cm.remove_component(comp)
        if unit is not None:
            room.unit_mgr.remove_unit(uid)
        # Unit destruction normally owns component destruction. Handle a detached one.
        if comp is not None and getattr(comp, '_owner', None) is not None:
            comp.destroy()
    except Exception as exc:
        errors.append('%s: %s' % (type(exc).__name__, exc))
    remaining = (room.unit_mgr.get_unit(uid) is not None or
                 ugc_id in cm._ugc_id2component or
                 any(ugc_id in ids for ids in cm._component_type2ugc_ids.values()))
    return {'complete': not errors and not remaining, 'errors': errors}


def get_edit_ui(edit):
    """取 UIBattleUGCEdit 实例。

    编辑器的「保存变更集」挂在它的 ugc_params['change_units'] 上，
    点保存时 _save_change 会遍历该集合收集要发给服务器的数据。
    """
    try:
        room = edit.get_room()
        if room is None:
            return None
        G = room.create_unit_with_info.__globals__
        ui = getattr(getattr(G.get('GK'), 'input', None), 'ugc_edit_ui', None)
        if ui is not None and isinstance(getattr(ui, 'ugc_params', None), dict):
            return ui
        gui = G.get('g_ui_mgr') or getattr(G.get('GK'), 'g_ui_mgr', None)
        if gui is None:
            return None
        ui = gui.get_ui('UIBattleUGCEdit')
        return ui if isinstance(getattr(ui, 'ugc_params', None), dict) else None
    except Exception:
        return None


def recover_pending(edit, args):
    """补做"服务端已创建、原生确认未完成"物件的登记。只走客户端命令层，不发服务端创建。

    成因（2026-09-20 实机，3182 件图）：place_item 与 place_poll 之间 room 被重建，
    新物件留在 room.del_unit_uids 的待确认态、不在 ui.all_ugc_uid_set；此时旧 request_id
    随 room 一起失效，place_poll 也会因它们已不在 ugc_params['create_units'] 里而报
    conflict —— 三条路都堵死，直接保存又会把它们漏掉（重进即消失）。
    这里重放的正是当年那一步 `ui._normal_create_units([unit], 当前位置)`：解除待删除标记、
    登记对象集合、更新组件数与占用、进撤销栈。它不建立服务端所有权、也不重发创建请求，
    因此不会造出重复件；持久化仍需保存 + 重进核实。默认 dry_run，写操作要显式传 false。
    """
    room, cm = context(edit)
    ui = get_edit_ui(edit)
    if ui is None:
        return {'error': '编辑器 UI 不可用'}
    want = [int(x) for x in (args.get('ugc_ids') or [])]
    dry = bool(args.get('dry_run', True))
    limit = int(args.get('limit') or 50)
    reg = set(getattr(ui, 'all_ugc_uid_set', set()) or set())
    del_set = set((getattr(room, 'del_unit_uids', {}) or {}).get('unit') or [])
    wset = set(want)
    candidates, skipped = [], []
    for gid, comp in sorted(cm._ugc_id2component.items()):
        if wset and gid not in wset:
            continue
        uid = getattr(comp, '_owner_uid', None)
        if uid is None:
            continue
        if uid in reg:
            if wset and gid in wset:
                skipped.append({'ugc_id': gid, 'owner_uid': uid, 'reason': '已登记，无需恢复'})
            continue
        if uid not in del_set:
            if wset and gid in wset:
                skipped.append({'ugc_id': gid, 'owner_uid': uid,
                                'reason': '不在待删除集合，状态未知，拒绝改动'})
            continue
        candidates.append({'ugc_id': gid, 'owner_uid': uid})
    wset and skipped.extend({'ugc_id': g, 'reason': '索引里查不到该 ugc_id'}
                            for g in sorted(wset - {c['ugc_id'] for c in candidates}
                                             - {s.get('ugc_id', -1) for s in skipped}))
    if len(candidates) > limit and not wset:
        return {'error': '待恢复物件 %d 个，超过单批上限 %d；请显式传 ugc_ids 分批'
                         % (len(candidates), limit)}
    report = {'dry_run': dry, 'pending_total': len(del_set), 'candidates': candidates,
              'skipped': skipped,
              'counters_before': {'component_nums': ui.get_all_ugc_component_nums(),
                                  'occupancy': ui.get_current_occupancy(),
                                  'registered': len(reg)}}
    if dry or not candidates:
        report['note'] = ('dry_run：只列出将恢复的物件。执行请传 dry_run=false；'
                          '恢复后必须立即 save_map 并核对 dropped_count。')
        return report
    done, failed = [], []
    for c in candidates:
        uid, gid = c['owner_uid'], c['ugc_id']
        unit = room.unit_mgr.get_unit(uid)
        if unit is None:
            failed.append(dict(c, error='实体不在 unit_mgr'))
            continue
        pos = (transform(unit) or {}).get('position')
        if not pos:
            failed.append(dict(c, error='读不到当前位置，拒绝确认'))
            continue
        ok, error = command_create(ui, unit, pos)
        reg_now = set(getattr(ui, 'all_ugc_uid_set', set()) or set())
        del_now = set((getattr(room, 'del_unit_uids', {}) or {}).get('unit') or [])
        if not ok or uid not in reg_now or uid in del_now:
            failed.append(dict(c, error=error or '确认后仍未登记或仍待删除'))
            break
        done.append(dict(c, position=pos))
    report.update(confirmed=done, failed=failed,
                  counters_after={'component_nums': ui.get_all_ugc_component_nums(),
                                  'occupancy': ui.get_current_occupancy(),
                                  'registered': len(set(getattr(ui, 'all_ugc_uid_set',
                                                                set()) or set())),
                                  'pending_total': len(set((getattr(room, 'del_unit_uids', {})
                                                            or {}).get('unit') or []))},
                  persistence_verified=False,
                  note='已补登记（客户端命令层）；持久化须 save_map 后重进核实。')
    if failed:
        report['error'] = '部分确认失败，已停止后续物件；现场保留，勿重试同一批。'
    return report


def command_create(ui, unit, position):
    """Confirm a server-created unit using native undo, membership and occupancy.

    This alone does not create server ownership or prove persistence.
    """
    if ui is None:
        return False, 'UI 实例不可用（无法执行创建命令）'
    try:
        import math3d
        end_pos = math3d.vector(*[float(v) for v in position])
    except Exception as exc:
        return False, '构造 math3d.vector 失败: %s' % exc
    try:
        ui._normal_create_units([unit], end_pos)
        return True, None
    except Exception as exc:
        return False, '%s: %s' % (type(exc).__name__, exc)


def register_change(ui, uid):
    """Register a change; this does not establish server ownership or save it."""
    if ui is None:
        return False, 'UI 实例不可用（无法登记保存变更集）'
    try:
        ui._add_change_unit(uid)
        return True, None
    except Exception as exc:
        return False, '%s: %s' % (type(exc).__name__, exc)


def register_create(ui, unit, comp):
    """模仿玩家手动放置时写入的新建记录。

    对照玩家手动放一个物件后的 ugc_params 实证：
      create_units      += [unit]        新建的 unit 对象
      create_components += [comp]        新建的组件对象
      has_create         = True
      change_units       由 _add_change_unit 维护
    而 save_component_info（本地重放队列）玩家路径**不写**，故本接口也不再写它。
    """
    if ui is None:
        return False, 'UI 实例不可用'
    try:
        up = getattr(ui, 'ugc_params', None)
        if not isinstance(up, dict):
            return False, 'ugc_params 不可用'
        up.setdefault('create_units', []).append(unit)
        up.setdefault('create_components', []).append(comp)
        up['has_create'] = True
        return True, None
    except Exception as exc:
        return False, '%s: %s' % (type(exc).__name__, exc)


def unregister_create(ui, unit, comp):
    """删除物件时清理新建记录。"""
    if ui is None:
        return False
    try:
        up = getattr(ui, 'ugc_params', None)
        if not isinstance(up, dict):
            return False
        hit = False
        for key, obj in (('create_units', unit), ('create_components', comp)):
            lst = up.get(key)
            if not isinstance(lst, list):
                continue
            for i in range(len(lst) - 1, -1, -1):
                if lst[i] is obj:
                    lst.pop(i)
                    hit = True
        if not up.get('create_units') and not up.get('create_components'):
            up['has_create'] = False
        return hit
    except Exception:
        return False


def unregister_change(ui, uid):
    """从保存变更集中移除 unit（删除物件时调用）。"""
    if ui is None:
        return False
    try:
        up = getattr(ui, 'ugc_params', None)
        if not isinstance(up, dict):
            return False
        cu = up.get('change_units')
        if not isinstance(cu, dict):
            return False
        hit = False
        for v in cu.values():
            if isinstance(v, set) and uid in v:
                v.discard(uid)
                hit = True
        return hit
    except Exception:
        return False


def change_units_snapshot(ui):
    """当前保存变更集（便于排查）。"""
    if ui is None:
        return None
    try:
        cu = (getattr(ui, 'ugc_params', None) or {}).get('change_units')
        if isinstance(cu, dict):
            return {str(k): sorted(int(x) for x in v
                                   if str(x).lstrip('-').isdigit())
                    for k, v in cu.items()}
    except Exception:
        pass
    return None


def save_event(spec, uid, ugc_id):
    """构造与游戏原生 add_unit 事件等价的事件负载。

    UGCComponentManager.check_and_save_component_info 会把该事件追加到
    save_component_info —— 即编辑器的待保存队列。

    ⚠️ 关键：do_save_component_info() 会把这个队列里的事件**重新执行**
    （handle_ugc_event(..., ignore_check=True)），所以负载必须能被
    room.ugc_add_unit 完整重放。ugc_recover_info 必须同时提供：
      - create_info    → 供 create_component_client 使用
      - component_info → 供 UGCBaseComponent.recover_ugc_info/init_ugc_info 使用
    缺 component_info 会让重放抛异常，进而使队列清空语句执行不到、保存队列
    永久卡住（表现为游戏内无法退出地图）。
    """
    return {
        'key': 'add_unit',
        'creat_params': [{
            'uid': uid,
            'unit_type': spec['unit_type'],
            'ugc_type': spec['item_id'],
            'model': spec['model_path'],
            'model_path': spec['model_path'],
            'pos': list(spec['position']),
            'ugc_dir': list(spec['rotation']),
            'ugc_size': list(spec['scale']),
            'scale': 1.0,
            'item_id': spec['item_id'],
            'ugc_recover_info': {
                'create_info': {
                    'component_type': spec['component_type'],
                    'owner_uid': uid,
                    'ugc_id': ugc_id,
                },
                'component_info': {
                    'component_id': spec['item_id'],
                    'events': [],
                },
            },
        }],
    }


def register_save(cm, event):
    """把事件登记进待保存队列；返回 (是否登记, 操作前标志)。

    异常向上传播，由调用方决定是否影响主流程。
    """
    was = bool(getattr(cm, 'is_save_component_info', False))
    try:
        cm.set_save_component_info(True)
        ok = bool(cm.check_and_save_component_info(event))
    finally:
        try:
            cm.set_save_component_info(was)
        except Exception:
            pass
    return ok, was


def pending_saves(cm):
    lst = getattr(cm, 'save_component_info', None)
    return list(lst) if isinstance(lst, list) else []


def _ugc_keys(cm):
    return {str(k) for k in (getattr(cm, '_ugc_id2component', {}) or {})}


def _find_new_server_unit(cm, before_keys, item_id):
    """在管理器里找服务器新建的 unit（ugc_type == item_id 且不在 before 中）。"""
    id2c = getattr(cm, '_ugc_id2component', {}) or {}
    target = int(item_id)
    for k, c in id2c.items():
        if str(k) in before_keys:
            continue
        u = getattr(c, '_owner', None)
        if u is None:
            continue
        try:
            if int(getattr(u, 'ugc_type', 0) or 0) == target:
                return u, c
        except (TypeError, ValueError):
            continue
    return None, None


def _apply_full_transform(unit, spec):
    import math3d
    # 2026-09-19 实测修正：BoxUnit/PanelUnit/CrowUnit/StairsUnit 等 21 个实体类
    # 没有 ugc_size 读属性（只有 set_ugc_size），旧实现此处抛 AttributeError，
    # 导致 finalize_failed 并把 23 个组件类型误判为「不可用/留残骸」。
    # 服务端本就受理这些类型；这里改为容错：读不到旧尺寸用 (1,1,1) 兜底。
    try:
        old_size = math3d.vector(unit.ugc_size)
    except AttributeError:
        old_size = math3d.vector(1, 1, 1)
    unit.set_ugc_size(math3d.vector(*spec['scale']), None, old_size)
    if hasattr(unit, 'update_col_ugc_size'):
        unit.update_col_ugc_size()
    unit.set_ugc_rot(math3d.euler_to_matrix(
        math3d.vector(*spec['rotation']) * (math.pi / 180)))
    # set_position 更新碰撞位置；set_ugc_position 更新逻辑位置
    #（_get_change_uid / 服务器读取的是逻辑位置）。
    unit.set_position(math3d.vector(*spec['position']))
    unit.set_ugc_position(math3d.vector(*spec['position']))


def apply_role_transform(unit, spec):
    """角色类（`ROLE_COMPONENT_TYPES`）的落位：只写位置。

    依据 EDIT_OPS.md §11：`MyCivilianUnit` / `MyButcherUnit` 没有 `ugc_size`/
    `ugc_dir` 读属性，套 `_apply_full_transform` 会走无关分支；角色的落位靠
    **创建时携带的 pos**（`_add_unit_server(..., pos=...)`），这里只做本地同步。
    Y 轴由系统落位决定（编辑器地图对角色没有可用 nav 高度场）。
    """
    import math3d
    p = math3d.vector(*[float(v) for v in spec['position']])
    unit.set_position(p)
    unit.set_ugc_position(p)


def role_position(unit):
    """角色的落位要读 `unit.position`。

    `get_position3()` 在角色处于"半隐藏未落位"态时返回 `HIDE_COL_POS=[0,-65536,0]`
    （EDIT_OPS.md §11.2），拿它当回读判据会把成功的创建误判成位置不符。
    这与快照侧 `pp` 的成因是同一个引擎行为。
    """
    p = unit.position
    return [float(p.x), float(p.y), float(p.z)]


def _placement_module():
    from fuxiang_reload import smart_reload
    return smart_reload('fuxiang_placement')


def place_poll(edit, args):
    import sys
    return _placement_module().poll(sys.modules[__name__], edit, args)


def _place_local(edit, spec, room, cm):
    """降级路径：客户端本地创建。物件不持久化，仅用于无 UI 时的预览。"""
    uid, ugc_id = next_unit_id(room, cm), next_component_id(cm)
    unit_info = {'uid': uid, 'unit_type': spec['unit_type'],
                 'ugc_type': spec['item_id'], 'model': spec['model_path'],
                 'pos': spec['position'], 'ugc_dir': spec['rotation'],
                 'ugc_size': spec['scale'], 'scale': 1.0}
    before = dict(cm._ugc_id2component)
    try:
        unit = room.create_unit_with_info(unit_info, None)
        if unit is None or room.unit_mgr.get_unit(uid) is not unit:
            raise RuntimeError('实体未注册到 unit_mgr')
        comp = cm.create_component_client({'component_type': spec['component_type'],
                                           'owner_uid': uid, 'ugc_id': ugc_id})
        if comp is None or cm._ugc_id2component.get(ugc_id) is not comp:
            raise RuntimeError('组件未注册到管理器')
        comp.init_ugc_info({'component_id': spec['item_id'], 'events': []})
        comp.init_ugc_info_finished()
        if any(cm._ugc_id2component.get(k) is not v for k, v in before.items()):
            raise RuntimeError('已有组件索引发生变化')
        if ugc_id not in cm._component_type2ugc_ids.get(spec['component_type'], set()):
            raise RuntimeError('组件类型索引缺失')
        actual = transform(unit)
        if any(any(abs(x-y) > 0.001 for x, y in zip(actual[k], spec[k]))
               for k in ('position', 'rotation', 'scale')):
            raise RuntimeError('实体变换与请求不一致')
        if unit.model is None or not unit.model.valid:
            raise RuntimeError('场景模型未就绪')
        unit._fuxiang_local_item = {'ugc_id': ugc_id, 'item_id': spec['item_id'],
                                    'server_created': False}
        cm._ugc_id_counter = max(cm._ugc_id_counter, ugc_id)
    except Exception as exc:
        return {'error': '%s: %s' % (type(exc).__name__, exc),
                'owner_uid': uid, 'ugc_id': ugc_id,
                'cleanup': cleanup(room, cm, uid, ugc_id)}
    return {'ugc_id': ugc_id, 'owner_uid': uid, 'unit_class': type(unit).__name__,
            'item_id': spec['item_id'], 'transform': actual,
            'scope': 'client_local', 'persistence_verified': False,
            'note': '客户端本地创建；此路径的物件不参与服务器保存'}


def place(edit, args):
    spec = plan(edit, args)
    room, cm = context(edit)
    ui = get_edit_ui(edit)
    mode = args.get('mode', 'server')
    if mode not in ('server', 'local_preview'):
        raise ValueError('mode 必须为 server 或 local_preview')
    if mode == 'local_preview':
        return _place_local(edit, spec, room, cm)
    if ui is not None and callable(getattr(ui, '_add_unit_server', None)):
        import sys
        return _placement_module().begin(sys.modules[__name__], edit, args, spec, room, cm, ui)
    raise ValueError('原生编辑器 UI 不可用；本地预览须显式传 mode=local_preview')


def place_batch(edit, args):
    """批量摆放：逐件走与 place(mode=server) 同一条 plan 校验，再整批登记。

    args: {'items': [{item_id, position, rotation, scale, request_id, mode,
                      allow_unverified, group_transforms, allow_out_of_range_scale},
                     ...]}
    每件独立 plan（白名单/缩放/成组/哨兵校验与单件路径逐字相同）；某件 plan
    失败只让那一件按「试了失败」定性（{'plan_error': 原因}），不拖垮整批。
    登记本身在 placement.begin_batch 里完成（护栏 1/2/3/4 见其 docstring）。
    """
    items = args.get('items')
    if not isinstance(items, list) or not items:
        raise ValueError('items 必须是非空数组')
    if len(items) > 128:
        raise ValueError('单批最多 128 件（MAX_REQUESTS）；请调小 --placement-batch')
    room, cm = context(edit)
    ui = get_edit_ui(edit)
    if ui is None or not callable(getattr(ui, '_add_unit_server', None)):
        raise ValueError('原生编辑器 UI 不可用，批量摆放需要编辑器上下文（server 模式）')
    planned = []
    for it in items:
        it = dict(it or {})
        if str(it.get('mode', 'server')) != 'server':
            planned.append({'plan_error': '批量摆放只支持 mode=server（local_preview '
                                          '不持久化，批量没有意义）'})
            continue
        try:
            spec = plan(edit, it)
            if not spec.get('server_begin_allowed'):
                raise ValueError(spec.get('server_begin_block_reason')
                                 or '该类型不在服务端摆放白名单')
            planned.append(spec)
        except Exception as exc:
            planned.append({'plan_error': '%s: %s' % (type(exc).__name__, exc)})
    import sys
    return _placement_module().begin_batch(sys.modules[__name__], edit, args,
                                           planned, room, cm, ui)


def place_poll_batch(edit, args):
    """批量 poll：一条命令推进整批请求（见 fuxiang_placement.poll_batch）。"""
    import sys
    return _placement_module().poll_batch(sys.modules[__name__], edit, args)


def owned_item(edit, args):
    room, cm = context(edit)
    value = args.get('ugc_id')
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError('ugc_id 必须为整数或数字字符串')
    ugc_id = int(value)
    comp = cm._ugc_id2component.get(ugc_id)
    unit = getattr(comp, '_owner', None)
    marker = getattr(unit, '_fuxiang_local_item', {})
    if marker.get('ugc_id') != ugc_id:
        raise ValueError('仅支持本接口创建且仍存活的本地物件')
    return room, cm, comp, unit


def del_event(uid, ugc_id):
    """删除事件负载。

    注意：本结构依据 room.ugc_del_unit 的字段引用（del_key / del_uid）构造，
    未经过服务端回环验证；仅用于「已保存过」的物件。
    """
    return {'key': 'del_unit', 'del_key': ugc_id, 'del_uid': ugc_id, 'uid': uid}


def _event_targets_ugc_id(event, ugc_id):
    """判断一条待保存的 add_unit 事件是否指向指定 ugc_id。"""
    if not isinstance(event, dict):
        return False
    for unit_info in event.get('creat_params') or []:
        if not isinstance(unit_info, dict):
            continue
        create_info = (unit_info.get('ugc_recover_info') or {}).get('create_info') or {}
        if create_info.get('ugc_id') == ugc_id:
            return True
    return False


def unregister_save(cm, ugc_id):
    """从待保存队列移除该物件的创建事件（等价于撤销一次尚未保存的创建）。

    返回被移除的事件条数。
    """
    lst = getattr(cm, 'save_component_info', None)
    if not isinstance(lst, list):
        return 0
    keep = [e for e in lst if not _event_targets_ugc_id(e, ugc_id)]
    removed = len(lst) - len(keep)
    if removed:
        lst[:] = keep
    return removed


def remove(edit, args):
    room, cm, comp, unit = owned_item(edit, args)
    uid, ugc_id = unit.uid, comp._ugc_id
    if unit._fuxiang_local_item.get('server_created'):
        raise ValueError('服务端物件不能用本地销毁清理；请在游戏内删除并保存')

    # 先从编辑器的保存记录中移除（须在销毁实体前，按对象引用匹配）
    ui = get_edit_ui(edit)
    create_removed = unregister_create(ui, unit, comp)
    change_removed = unregister_change(ui, uid)

    result = cleanup(room, cm, uid, ugc_id)
    if not result['complete']:
        return {'error': '物件未完全清理', 'cleanup': result,
                'owner_uid': uid, 'ugc_id': ugc_id}

    return {'removed': True, 'owner_uid': uid, 'ugc_id': ugc_id,
            'create_set_removed': create_removed,
            'change_set_removed': change_removed,
            'change_units': change_units_snapshot(ui)}


def set_transform(edit, args):
    room, cm, comp, unit = owned_item(edit, args)
    keys = ('position', 'rotation', 'scale')
    if not any(k in args for k in keys):
        raise ValueError('至少提供 position / rotation / scale 中一项')
    before = transform(unit)
    requested = dict(before)
    requested.update({k: args[k] for k in keys if k in args})
    spec = plan(edit, dict(requested, item_id=unit._fuxiang_local_item['item_id'],
                           allow_unverified=True))
    is_server = bool(unit._fuxiang_local_item.get('server_created'))
    ui = get_edit_ui(edit) if is_server else None
    if is_server and ui is None:
        raise ValueError('编辑器 UI 不可用，未修改服务端物件')

    def apply(values):
        _apply_full_transform(unit, values)
        if is_server:
            ui._add_change_unit(unit.uid)

    try:
        apply(spec)
        after = transform(unit)
        for key in keys:
            for actual, wanted in zip(after[key], spec[key]):
                delta = actual - wanted
                if key == 'rotation':
                    delta = (delta + 180) % 360 - 180
                if abs(delta) > .001:
                    raise RuntimeError('变换回读与请求不一致: %s' % key)
        return {'ugc_id': comp._ugc_id, 'before': before, 'transform': after,
                'scope': 'server' if is_server else 'client_local', 'persistence_verified': False}
    except Exception as exc:
        rollback = {'complete': False}
        try:
            apply(before)
            restored = transform(unit)
            rollback['complete'] = all(abs(x-y) < .001 for k in keys
                                       for x, y in zip(restored[k], before[k]))
        except Exception as rollback_exc:
            rollback['error'] = str(rollback_exc)
        return {'error': str(exc), 'rollback': rollback, 'ugc_id': comp._ugc_id}
