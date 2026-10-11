using FuxiangConsole.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Collections.ObjectModel;
using System.Text.Json.Nodes;

namespace FuxiangConsole.Pages;

public sealed partial class ConsolePage : Page
{
    public ObservableCollection<string> OpNames { get; } = new(CommonOps);

    static readonly string[] CommonOps =
    {
        "status", "map_info", "list_items", "list_types", "list_components", "get_component",
        "catalog_find", "catalog_types", "catalog_schema", "catalog_enums", "catalog_items",
        "plan_item", "place_item", "place_poll", "set_transform", "edit_targets", "edit_target",
        "edit_transform", "edit_props", "edit_history", "delete_preview", "delete_objects",
        "event_list", "event_create", "event_bind", "event_unbind", "get_params", "set_params",
        "story_tree", "story_fields", "story_set", "story_create", "story_create_poll",
        "story_export", "story_import", "story_import_next", "save_map", "save_map_poll",
        "save_dirty", "pending_saves", "save", "set_map_setting", "get_map_bgm", "set_map_bgm",
        "global_singletons", "ui_state", "ui_parts", "ui_list", "cover_state",
        "preview_plan", "preview_shot", "eval",
    };

    static readonly (string op, string args)[] Presets =
    {
        ("status", "{}"),
        ("map_info", "{}"),
        ("save_dirty", "{}"),
        ("catalog_find", "{\"query\":\"桌子\",\"limit\":3}"),
        ("list_items", "{\"limit\":10}"),
        ("story_tree", "{\"compact\":true}"),
        ("event_list", "{}"),
        ("ui_state", "{}"),
    };

    public ConsolePage()
    {
        InitializeComponent();
        OpBox.ItemsSource = OpNames;
        foreach (var (op, args) in Presets)
        {
            var b = new Button { Content = op, FontSize = 11, Padding = new Thickness(8, 2, 8, 2), Tag = args };
            b.Click += (_, _) => { OpBox.Text = op; ArgsBox.Text = args; };
            PresetPanel.Children.Add(b);
        }
        RenderManual("");
        RenderHist();
    }

    // ---- 命令手册 ----
    sealed record OpDoc(string Name, string Desc, string Format);

    static readonly string[] ManualCatOrder =
    {
        "状态与保存", "场景与物件", "目录与摆放", "事件", "剧情",
        "界面编辑器", "封面", "相机与截图", "资源大厅", "逃生舱",
    };

    static readonly Dictionary<string, string> OpCat = new()
    {
        ["status"] = "状态与保存", ["map_info"] = "状态与保存", ["save_dirty"] = "状态与保存",
        ["save_map"] = "状态与保存", ["save_map_poll"] = "状态与保存", ["pending_saves"] = "状态与保存",
        ["save"] = "状态与保存", ["set_map_setting"] = "状态与保存",
        ["get_map_bgm"] = "状态与保存", ["set_map_bgm"] = "状态与保存",

        ["list_items"] = "场景与物件", ["list_types"] = "场景与物件", ["list_components"] = "场景与物件",
        ["get_component"] = "场景与物件", ["edit_targets"] = "场景与物件", ["edit_target"] = "场景与物件",
        ["edit_transform"] = "场景与物件", ["edit_props"] = "场景与物件", ["edit_history"] = "场景与物件",
        ["set_transform"] = "场景与物件", ["remove_item"] = "场景与物件", ["delete_preview"] = "场景与物件",
        ["delete_objects"] = "场景与物件", ["recover_pending"] = "场景与物件", ["global_singletons"] = "场景与物件",

        ["catalog_find"] = "目录与摆放", ["catalog_types"] = "目录与摆放", ["catalog_items"] = "目录与摆放",
        ["catalog_schema"] = "目录与摆放", ["catalog_enums"] = "目录与摆放", ["catalog_verify"] = "目录与摆放",
        ["plan_item"] = "目录与摆放", ["place_item"] = "目录与摆放", ["place_poll"] = "目录与摆放",
        ["place_batch"] = "目录与摆放",

        ["event_list"] = "事件", ["event_create"] = "事件", ["event_delete"] = "事件",
        ["event_bind"] = "事件", ["event_unbind"] = "事件", ["get_params"] = "事件", ["set_params"] = "事件",

        ["story_tree"] = "剧情", ["story_fields"] = "剧情", ["story_set"] = "剧情",
        ["story_create"] = "剧情", ["story_create_poll"] = "剧情", ["story_delete"] = "剧情",
        ["story_export"] = "剧情", ["story_import"] = "剧情", ["story_import_next"] = "剧情",

        ["ui_state"] = "界面编辑器", ["ui_parts"] = "界面编辑器",
        ["ui_list"] = "界面编辑器", ["ui_get"] = "界面编辑器", ["ui_add"] = "界面编辑器",

        ["cover_state"] = "封面", ["cover_set_slot"] = "封面", ["cover_upload"] = "封面",
        ["cover_upload_poll"] = "封面", ["cover_apply"] = "封面",

        ["preview_plan"] = "相机与截图", ["preview_pose"] = "相机与截图", ["preview_fire"] = "相机与截图",
        ["preview_restore"] = "相机与截图", ["preview_state"] = "相机与截图",

        ["hall_info"] = "资源大厅", ["hall_items"] = "资源大厅",
        ["combo_download"] = "资源大厅", ["combo_download_status"] = "资源大厅",

        ["eval"] = "逃生舱",
    };

    static readonly OpDoc[] OpDocs =
    {
        new("status", "编辑器核心 / 组件·事件管理器 / 服务器会话状态总览", "{}"),
        new("map_info", "地图概况：名称、简介、玩法设置、组件类型分布", "{}"),
        new("save_dirty", "查脏缓冲：有没有还没保存的改动（只读）", "{}"),
        new("save_map", "发起一次真实保存（与游戏保存按钮同链路），随后要轮询", "{}"),
        new("save_map_poll", "轮询保存回执（status=saved 即完成）", "{\"request_id\":\"save_xxx\"}"),
        new("pending_saves", "查看本地事件重放队列（不是保存回执）", "{}"),
        new("save", "旧版标记/改名接口（≠真实保存）", "{}"),
        new("list_items", "按名称搜场景里的物件", "{\"query\":\"桌子\",\"limit\":10}"),
        new("list_types", "场景里所有组件类型和数量", "{}"),
        new("list_components", "列出组件明细", "{\"limit\":20}"),
        new("get_component", "看单个组件详情", "{\"ugc_id\":123}"),
        new("catalog_find", "在物件目录里搜可摆放的东西", "{\"query\":\"桌子\",\"limit\":5}"),
        new("catalog_types", "物件目录里的全部类型", "{}"),
        new("catalog_items", "分页浏览目录物件", "{\"limit\":20}"),
        new("catalog_schema", "某类组件的可编辑字段表", "{\"component_type\":203003}"),
        new("catalog_enums", "字段枚举值对照表", "{}"),
        new("catalog_verify", "校验某个物件能否摆放", "{\"item_id\":202014}"),
        new("plan_item", "只读预演：检查这样摆可不可行（不写入）", "{\"item_id\":202009,\"position\":[0,0,0]}"),
        new("place_item", "摆放物件（服务端受理，随后要 place_poll）", "{\"item_id\":202009,\"position\":[0,0,0],\"request_id\":\"a1\",\"mode\":\"server\"}"),
        new("place_poll", "查询摆放结果（status=placed 即成功）", "{\"request_id\":\"a1\"}"),
        new("place_batch", "批量摆放（快照还原提速）", "{\"items\":[{\"item_id\":202009,\"position\":[0,0,0],\"request_id\":\"b1\"}]}"),
        new("set_transform", "改物件的位置/旋转/缩放（服务端物件登记变更）", "{\"ugc_id\":123,\"position\":[0,0,0],\"rotation\":[0,30,0],\"scale\":1}"),
        new("remove_item", "只销毁本地预览件（服务端物件要删请用 delete_objects）", "{\"ugc_id\":123}"),
        new("edit_targets", "列出所有可编辑物件（limit 0 = 全量）", "{\"limit\":0}"),
        new("edit_target", "看某个物件的可编辑字段与当前值", "{\"ugc_id\":123}"),
        new("edit_transform", "带撤销的变换编辑", "{\"ugc_id\":123,\"position\":[0,0,0]}"),
        new("edit_props", "改属性袋（原生三步）", "{\"ugc_id\":123,\"props\":{\"20300411201\":[\"文本\"]}}"),
        new("edit_history", "本会话经接口做过的数值改动", "{}"),
        new("delete_preview", "只读预览：会删到什么（含组合级联）", "{\"ugc_ids\":[123]}"),
        new("delete_objects", "原生命令层删除对象（游戏内可撤销）", "{\"ugc_ids\":[123]}"),
        new("event_list", "场景事件键列表", "{}"),
        new("event_create", "新建事件键", "{\"key\":\"我的事件\"}"),
        new("event_delete", "删除事件键", "{\"key\":\"我的事件\"}"),
        new("event_bind", "绑定（info 按原始形态整段写回）", "{\"key\":\"我的事件\",\"ugc_id\":123,\"property_id\":\"20300311106\",\"info\":[\"我的事件\",1286]}"),
        new("event_unbind", "解绑", "{\"key\":\"我的事件\",\"ugc_id\":123,\"property_id\":\"20300311106\"}"),
        new("get_params", "读场景 params 事件键（只读）", "{}"),
        new("set_params", "改 params", "{\"child_key\":0,\"key\":\"xxx\",\"info\":{}}"),
        new("story_tree", "剧情树（compact 只报数量）", "{\"compact\":true}"),
        new("story_fields", "剧情可写字段字典", "{\"kind\":\"story\"}"),
        new("story_set", "改剧情节点字段", "{\"ugc_id\":123,\"fields\":{\"name\":\"新名\"},\"dry_run\":true}"),
        new("story_create", "新建剧情节点（随后 story_create_poll）", "{\"kind\":\"story\",\"fields\":{\"name\":\"新剧情\"}}"),
        new("story_create_poll", "查询剧情新建结果", "{\"request_id\":\"xxx\"}"),
        new("story_delete", "删剧情节点（非登记节点要 force）", "{\"ugc_ids\":[123],\"force\":true,\"dry_run\":true}"),
        new("story_export", "导出剧情文件（json/md）", "{\"path\":\"D:/out.json\",\"format\":\"json\"}"),
        new("story_import", "导入剧情（默认 dry-run 出计划）", "{\"path\":\"D:/in.json\",\"apply\":false,\"prune\":false}"),
        new("story_import_next", "推进导入创建链", "{\"session_id\":\"xxx\"}"),
        new("set_map_setting", "写玩法设置键（\"14\"=场景框架 4001/4002/4003）", "{\"key\":\"14\",\"value\":\"4001\"}"),
        new("get_map_bgm", "读地图默认背景音乐", "{}"),
        new("set_map_bgm", "写地图默认背景音乐", "{}"),
        new("global_singletons", "查看全局单例对象", "{}"),
        new("ui_state", "界面编辑器状态", "{}"),
        new("ui_parts", "界面编辑器 19 个面板定位", "{}"),
        new("ui_list", "画布控件列表", "{}"),
        new("ui_get", "看控件属性", "{\"ugc_id\":123}"),
        new("ui_add", "新增界面控件（随后 ui_add_poll）", "{}"),
        new("cover_state", "封面槽位 + 服务器回读", "{}"),
        new("cover_set_slot", "落图并写封面槽", "{}"),
        new("cover_upload", "发起封面上传", "{}"),
        new("cover_upload_poll", "轮询上传回执", "{}"),
        new("cover_apply", "封面落库（set_ugc_map_cover_photo）", "{\"urls\":[\"https://...\"]}"),
        new("preview_plan", "出图规划（可用视角列表）", "{\"fit\":\"content\"}"),
        new("preview_pose", "摆相机到指定视角", "{}"),
        new("preview_fire", "触发引擎截图落盘", "{\"path\":\"D:/x.png\",\"format\":3}"),
        new("preview_restore", "还原相机", "{}"),
        new("preview_state", "相机当前状态", "{}"),
        new("hall_info", "资源大厅（组合库）概况", "{}"),
        new("hall_items", "资源大厅组合列表", "{\"limit\":10}"),
        new("combo_download", "下载组合到账号库（随后 combo_download_poll）", "{}"),
        new("combo_download_status", "组合下载状态", "{}"),
        new("recover_pending", "恢复\"服务端已建、原生确认未完成\"的物件（默认 dry-run）", "{\"dry_run\":true}"),
        new("eval", "在游戏主线程执行 Python（逃生舱，预置 core/room/cm/em/GK）", "{\"code\":\"__result__ = str(room)\"}"),
    };

    static readonly Microsoft.UI.Xaml.Media.FontFamily MonoFont = new("Cascadia Mono");

    void RenderManual(string filter)
    {
        ManualList.Children.Clear();
        filter = (filter ?? "").Trim();
        bool searching = filter.Length > 0;
        foreach (var cat in ManualCatOrder)
        {
            var docs = OpDocs
                .Where(d => OpCat.TryGetValue(d.Name, out var c) && c == cat)
                .Where(d => filter.Length == 0
                            || d.Name.Contains(filter, StringComparison.OrdinalIgnoreCase)
                            || d.Desc.Contains(filter, StringComparison.OrdinalIgnoreCase))
                .ToArray();
            if (docs.Length == 0) continue;

            var panel = new StackPanel { Spacing = 4 };
            foreach (var d in docs) panel.Children.Add(ManualRow(d));

            ManualList.Children.Add(new Expander
            {
                Header = new TextBlock
                {
                    Text = $"{cat}（{docs.Length}）",
                    FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                },
                Content = panel,
                IsExpanded = searching,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
            });
        }
        if (ManualList.Children.Count == 0)
            ManualList.Children.Add(new TextBlock
            {
                Text = "没有匹配的命令。",
                Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.Gray),
                Margin = new Thickness(0, 4, 0, 4),
            });
    }

    FrameworkElement ManualRow(OpDoc d)
    {
        var b = new Button
        {
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            Padding = new Thickness(12, 8, 12, 8),
        };
        var name = new TextBlock
        {
            Text = d.Name,
            FontFamily = MonoFont,
            FontSize = 13,
            FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
            VerticalAlignment = VerticalAlignment.Top,
        };
        var right = new StackPanel { Spacing = 2 };
        right.Children.Add(new TextBlock
        {
            Text = d.Desc, FontSize = 12, Opacity = 0.85, TextWrapping = TextWrapping.Wrap,
        });
        right.Children.Add(new TextBlock
        {
            Text = d.Format, FontFamily = MonoFont, FontSize = 11.5, Opacity = 0.65,
            TextWrapping = TextWrapping.Wrap,
        });
        var g = new Grid { ColumnSpacing = 12 };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(150) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        Grid.SetColumn(name, 0);
        Grid.SetColumn(right, 1);
        g.Children.Add(name);
        g.Children.Add(right);
        b.Content = g;
        b.Click += (_, _) =>
        {
            OpBox.Text = d.Name;
            ArgsBox.Text = d.Format;
            ResultBox.Text = "已填入「" + d.Name + "」，按「执行 op」运行。";
        };
        return b;
    }

    void ManualSearch_TextChanged(object sender, TextChangedEventArgs e) => RenderManual(ManualSearch.Text);

    async void OpRun_Click(AutoSuggestBox sender, AutoSuggestBoxQuerySubmittedEventArgs args) => await RunOp();
    async void OpRun_Click(object sender, RoutedEventArgs e) => await RunOp();

    void OpBox_TextChanged(AutoSuggestBox sender, AutoSuggestBoxTextChangedEventArgs args) { }

    async Task RunOp()
    {
        var op = OpBox.Text.Trim();
        if (op.Length == 0) { ResultBox.Text = "op 名为空"; return; }
        JsonNode args = null;
        var raw = ArgsBox.Text.Trim();
        if (raw.Length > 0)
        {
            try { args = JsonNode.Parse(raw); }
            catch (Exception ex) { ResultBox.Text = "参数不是合法 JSON：" + ex.Message; return; }
        }
        OpRing.IsActive = true;
        ResultBox.Text = "执行中…";
        try
        {
            var v = await Fx.CallOp(op, args);
            ResultBox.Text = Ui.Pretty(v);
            PushHist($"op {op} {raw}");
        }
        catch (Exception ex) { ResultBox.Text = "执行失败：" + ex.Message; }
        finally { OpRing.IsActive = false; }
    }

    async void EvalRun_Click(object sender, RoutedEventArgs e)
    {
        var code = EvalBox.Text;
        if (string.IsNullOrWhiteSpace(code)) return;
        EvalRing.IsActive = true;
        ResultBox.Text = "执行中…";
        try
        {
            var v = await Fx.Eval(code, EvalJsonChk.IsChecked == true);
            ResultBox.Text = Ui.Pretty(v);
            PushHist("eval " + code.Replace("\n", " ").Trim());
        }
        catch (Exception ex) { ResultBox.Text = "执行失败：" + ex.Message; }
        finally { EvalRing.IsActive = false; }
    }

    void PushHist(string item)
    {
        var s = Settings.Load();
        s.ConsoleHistory.Insert(0, item);
        if (s.ConsoleHistory.Count > 50) s.ConsoleHistory = s.ConsoleHistory.Take(50).ToList();
        s.Save();
        RenderHist();
    }

    void RenderHist()
    {
        HistList.Items.Clear();
        foreach (var h in Settings.Load().ConsoleHistory)
            HistList.Items.Add(new TextBlock { Text = h, FontSize = 11.5, TextTrimming = TextTrimming.CharacterEllipsis });
    }

    void Hist_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (HistList.SelectedItem is not TextBlock tb) return;
        var text = tb.Text;
        if (text.StartsWith("op "))
        {
            var rest = text[3..];
            int i = rest.IndexOf(' ');
            OpBox.Text = i < 0 ? rest : rest[..i];
            ArgsBox.Text = i < 0 ? "{}" : rest[(i + 1)..];
        }
        else if (text.StartsWith("eval "))
        {
            EvalBox.Text = text[5..];
        }
        HistList.SelectedItem = null;
    }

    void BtnClearHist_Click(object sender, RoutedEventArgs e)
    {
        var s = Settings.Load();
        s.ConsoleHistory.Clear();
        s.Save();
        RenderHist();
    }
}
