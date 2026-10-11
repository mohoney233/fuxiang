using FuxiangConsole.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;

namespace FuxiangConsole.Pages;

public sealed partial class TeamPage : Page
{
    const string ReadCode = """
        import json as _j
        try:
            props = getattr(room, 'ugc_props_info', None) or {}
            out = {'ok': True,
                   'player_num': props.get('player_num'),
                   'camp_info': props.get('camp_info'),
                   'prop_keys': sorted(map(str, props.keys())) if isinstance(props, dict) else None}
        except BaseException as e:
            out = {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}
        __result__ = _j.dumps(out, ensure_ascii=False, default=str)
        """;

    public TeamPage()
    {
        InitializeComponent();
    }

    async void BtnRead_Click(object sender, RoutedEventArgs e)
    {
        ReadRing.IsActive = true;
        try
        {
            var v = await Fx.Eval(ReadCode, jsonResult: true) as JsonObject;
            if (v == null || v["ok"]?.GetValue<bool>() != true)
            {
                TeamsView.Text = "读取失败：" + (v?["error"]?.ToString() ?? "null");
                TechOpen();
                TeamsSummary.Text = "读取失败，详见「技术详情」。";
                return;
            }
            // ---- 友好摘要 ----
            TeamsFriendly.Children.Clear();
            var camps = v["camp_info"] as JsonArray ?? new JsonArray();
            int civTotal = 0, butchTotal = 0;
            var slots = new List<string>();
            int i = 0;
            foreach (var c in camps)
            {
                var co = c as JsonObject;
                i++;
                int civ = (co?["civilian_num"] as JsonValue)?.TryGetValue<double>(out var cd) == true ? (int)cd : 0;
                int butch = (co?["butcher_num"] as JsonValue)?.TryGetValue<double>(out var bd) == true ? (int)bd : 0;
                civTotal += civ; butchTotal += butch;
                var card = new Border
                {
                    Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.White),
                    BorderBrush = new Microsoft.UI.Xaml.Media.SolidColorBrush(Windows.UI.Color.FromArgb(255, 210, 210, 210)),
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(8),
                    Padding = new Thickness(12, 8, 12, 8),
                };
                var sp = new StackPanel { Spacing = 4 };
                sp.Children.Add(new TextBlock
                {
                    Text = $"队伍 {i}（{co?["name"] ?? "未命名"}）",
                    FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                    FontSize = 13.5,
                });
                sp.Children.Add(new TextBlock
                {
                    Text = $"求生者 {civ} 人 · 监管者 {butch} 人",
                    FontSize = 13,
                });
                var slotIds = (co?["civilian_idx_lst"] as JsonArray ?? new JsonArray()).Select(x => x?.ToString()).Where(s => s != null).ToList();
                if (slotIds.Count > 0)
                    sp.Children.Add(new TextBlock
                    {
                        Text = "求生者槽位：" + string.Join("、", slotIds),
                        FontSize = 12, Opacity = 0.8, TextWrapping = TextWrapping.Wrap,
                    });
                foreach (var s in slotIds) slots.Add(s);
                card.Child = sp;
                TeamsFriendly.Children.Add(card);
            }
            TeamsSummary.Text = $"游戏人数上限 {v["player_num"]?.ToJsonString() ?? "?"} · {camps.Count} 支队伍 · 求生者槽位 {civTotal} 个 · 监管者槽位 {butchTotal} 个";

            // ---- 原始数据（折叠区） ----
            var lines = new List<string>();
            foreach (var c in camps)
            {
                var co = c as JsonObject;
                lines.Add($"—— 队伍：{co?["name"]} ——");
                lines.Add($"  civilian_num={co?["civilian_num"]} butcher_num={co?["butcher_num"]}");
                lines.Add($"  civilian_idx_lst={co?["civilian_idx_lst"]?.ToJsonString()}");
                lines.Add($"  civilian_class_lst={co?["civilian_class_lst"]?.ToJsonString()}");
            }
            lines.Add("");
            var keys = v["prop_keys"] as JsonArray;
            lines.Add("props 全键：" + (keys != null ? string.Join(", ", keys.Select(x => x?.ToString())) : "（不可用）"));
            lines.Add("");
            lines.Add(Ui.Pretty(v));
            TeamsView.Text = string.Join("\n", lines);

            // 填充删除下拉框
            DelSlot.Items.Clear();
            foreach (var s in slots) DelSlot.Items.Add($"槽位 {s}");
            DelSlot.PlaceholderText = slots.Count > 0 ? $"共 {slots.Count} 个槽位，选一个" : "（没有可用槽位）";
        }
        catch (Exception ex)
        {
            TeamsView.Text = "读取失败：" + ex.Message;
            TechOpen();
            TeamsSummary.Text = "读取失败，详见「技术详情」。";
        }
        finally { ReadRing.IsActive = false; }
    }

    void TechOpen()
    {
        // 出问题时自动展开技术详情，方便反馈
        var exp = Ui.FindAncestorExpander(TeamsView);
        if (exp != null) exp.IsExpanded = true;
    }

    /// <summary>删除目标：优先下拉框选择，其次手动输入。</summary>
    int? ChosenSlot()
    {
        if (DelSlot.SelectedIndex >= 0)
        {
            var t = DelSlot.SelectedItem as string;
            var num = t?.Replace("槽位 ", "");
            if (int.TryParse(num, out int a)) return a;
        }
        if (int.TryParse(DelId.Text.Trim(), out int b)) return b;
        return null;
    }

    async void BtnAdd_Click(object sender, RoutedEventArgs e)
    {
        double[] pos;
        try
        {
            pos = JsonNode.Parse(PosBox.Text).AsArray().Select(n => (double)n).ToArray();
            if (pos.Length != 3) throw new Exception("需要 3 个数");
        }
        catch (Exception ex) { await Ui.Info(this, "坐标不合法", "位置要写成 [x, y, z] 三个数字，例如 [0, 2, 0]。\n" + ex.Message); return; }

        var roles = new List<(int id, string label)>();
        if (CivChk.IsChecked == true) roles.Add((101001, "求生者"));
        if (ButchChk.IsChecked == true) roles.Add((102001, "监管者"));
        if (roles.Count == 0) { await Ui.Info(this, "提示", "至少勾选一种槽位（求生者或监管者）。"); return; }

        if (!await Ui.Confirm(this, "添加槽位",
            $"将在位置 [{pos[0]}, {pos[1]}, {pos[2]}] 放置：{string.Join(" + ", roles.Select(r => r.label))} 槽位。\n放置后需要保存地图才生效。继续？"))
            return;

        AddRing.IsActive = true;
        var anyOk = false;
        try
        {
            foreach (var (id, label) in roles)
            {
                Ui.Log(LogBox, $"创建 {label} 槽位（item_id={id}）…");
                var r = await Fx.TeamAddSlot(id, pos, label);
                bool ok = r["ok"]?.GetValue<bool>() ?? false;
                anyOk |= ok;
                Ui.Log(LogBox, ok
                    ? $"[完成] {label} 槽位已创建并登记"
                    : $"[失败] {label}：{r.ToJsonString()}");
                if (!ok) break;
            }
            if (anyOk)
            {
                Ui.Banner(ResultBar, "槽位已创建", "现在需要保存地图才会生效。", true);
                await AskSave();
            }
            else
                Ui.Banner(ResultBar, "创建失败", "详见任务日志。", false);
        }
        catch (Exception ex) { Ui.Banner(ResultBar, "创建失败", ex.Message, false); }
        finally { AddRing.IsActive = false; }
    }

    async Task AskSave()
    {
        if (await Ui.SaveReminder(this))
        {
            try
            {
                var r = await Fx.SaveMapFlow();
                Ui.Log(LogBox, r["ok"]?.GetValue<bool>() == true ? "[完成] 服务端已回执保存" : "[失败] " + r.ToJsonString());
                if (r["ok"]?.GetValue<bool>() == true)
                    Ui.Banner(ResultBar, "已保存", "地图已写入游戏存档。", true);
                else
                    Ui.Banner(ResultBar, "保存失败", "详见任务日志。", false);
            }
            catch (Exception ex) { Ui.Banner(ResultBar, "保存失败", ex.Message, false); }
        }
    }

    async void BtnSave_Click(object sender, RoutedEventArgs e) => await AskSave();

    async void BtnDelPreview_Click(object sender, RoutedEventArgs e)
    {
        var id = ChosenSlot();
        if (id == null) { await Ui.Info(this, "先选槽位", "先点「读取队伍数据」，然后在下拉框里选要删的槽位（或手动输入编号）。"); return; }
        try
        {
            var v = await Fx.CallOp("delete_preview", new JsonObject
            {
                ["ugc_ids"] = new JsonArray(id.Value),
            });
            TeamsView.Text = $"删除预览（还没真的删）：\n" + Ui.Pretty(v);
            TechOpen();
            var cnt = (v?["would_delete_count"] as JsonValue)?.TryGetValue<double>(out var n) == true ? (int)n : -1;
            if (cnt == 0)
                Ui.Banner(ResultBar, "预览结果：没有可删的对象", "该编号当前不可删除（可能不存在或被护栏拦下），详见技术详情。", false);
            else
                Ui.Banner(ResultBar, $"预览结果：将删除 {cnt} 个对象（还没真的删）", "确认无误后点「删除」。", true);
        }
        catch (Exception ex) { TeamsView.Text = "预览失败：" + ex.Message; TechOpen(); }
    }

    async void BtnDel_Click(object sender, RoutedEventArgs e)
    {
        var id = ChosenSlot();
        if (id == null) { await Ui.Info(this, "先选槽位", "先点「读取队伍数据」，然后在下拉框里选要删的槽位（或手动输入编号）。"); return; }
        if (!await Ui.Confirm(this, "删除槽位",
            $"确定删除槽位 {id}？删除后需要在游戏里保存才生效（游戏内可 Ctrl+Z 撤销）。")) return;
        try
        {
            var v = await Fx.CallOp("delete_objects", new JsonObject
            {
                ["ugc_ids"] = new JsonArray(id.Value),
            });
            TeamsView.Text = "删除结果：\n" + Ui.Pretty(v);
            TechOpen();
            Ui.Banner(ResultBar, "已删除", "记得保存地图让删除生效。", true);
            await AskSave();
        }
        catch (Exception ex) { TeamsView.Text = "删除失败：" + ex.Message; TechOpen(); }
    }
}
