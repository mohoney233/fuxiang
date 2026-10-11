using FuxiangConsole.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;

namespace FuxiangConsole.Pages;

public sealed partial class EventPage : Page
{
    public EventPage()
    {
        InitializeComponent();
        Loaded += (_, _) => { ReloadFiles(); ShowDefaultFile(); };
    }

    void ReloadFiles()
    {
        var files = Ui.ListFiles(Fx.EventDir, ".json");
        ImportFile.Items.Clear();
        DslSrc.Items.Clear();
        foreach (var f in files)
        {
            ImportFile.Items.Add(f.Path);
            DslSrc.Items.Add(f.Path);
        }
        if (ImportFile.Items.Count > 0) ImportFile.SelectedIndex = 0;
        if (DslSrc.Items.Count > 0) DslSrc.SelectedIndex = 0;
    }

    void ShowDefaultFile()
    {
        var p = Path.Combine(Fx.EventDir, "map_events.json");
        if (File.Exists(p)) _ = ShowFile(p);
    }

    async Task ShowFile(string path)
    {
        SrcName.Text = "（" + Short(path) + "）";
        try
        {
            var txt = await File.ReadAllTextAsync(path);
            var node = JsonNode.Parse(txt);
            EventView.Text = Ui.Pretty(node);
            RenderFriendly(node, Short(path));
        }
        catch (Exception ex)
        {
            FriendlySummary.Text = "读取失败：" + ex.Message;
            EventView.Text = "读取失败：" + ex.Message;
        }
    }

    /// <summary>把事件导出渲染成人话：一行摘要 + 每个事件一张小卡。</summary>
    void RenderFriendly(JsonNode node, string fileName)
    {
        FriendlyList.Children.Clear();
        if (node is not JsonObject o)
        { FriendlySummary.Text = "（文件格式不是标准的事件导出）"; return; }
        var objs = o["objects"] as JsonObject;
        var evsNode = o["events"];
        var evs = evsNode as JsonArray ?? (evsNode as JsonObject)?["events"] as JsonArray;
        var meta = o["meta"] as JsonObject;
        int bindTotal = 0;
        foreach (var e in evs ?? new JsonArray())
            bindTotal += (e as JsonObject)?["bindings"] as JsonArray is JsonArray b ? b.Count : 0;

        var exported = meta?["exported_at"]?.ToString();
        FriendlySummary.Text = $"共 {(evs?.Count ?? 0)} 组事件（{bindTotal} 条联动）· {objs?.Count ?? 0} 个可触发物件" +
                               (exported != null ? $" · 导出于 {exported}" : "") + $" · 文件 {fileName}" +
                               "\n每张小卡是一个事件：谁在什么情况下触发、效果是什么。";

        int shown = 0;
        foreach (var e in evs ?? new JsonArray())
        {
            if (shown >= 60)
            {
                var more = new TextBlock
                {
                    Text = $"… 其余 {(evs?.Count ?? 0) - shown} 组事件在「技术详情」里查看",
                    Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(
                        Microsoft.UI.Colors.Gray),
                    Margin = new Thickness(0, 2, 0, 2),
                };
                FriendlyList.Children.Add(more);
                break;
            }
            var eo = e as JsonObject;
            var key = eo?["key"]?.ToString() ?? "(未命名事件)";
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
                Text = key,
                FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                FontSize = 13.5,
            });
            foreach (var b in eo?["bindings"] as JsonArray ?? new JsonArray())
            {
                if (b is not JsonObject bo) continue;
                var role = bo["role"]?.ToString() switch
                {
                    "sender" => "触发",
                    "receiver" => "接收",
                    _ => "关联",
                };
                var obj = bo["object"]?.ToString() ?? "?";
                var sem = bo["semantic"]?.ToString() ?? "";
                var line = $"{role}：{obj}　{sem}";
                if (bo["value"] is JsonObject vo && vo.Count > 0)
                {
                    var parts = vo.Select(kv => $"{kv.Key}: {kv.Value?.ToString()}").Take(3);
                    line += "　→　" + string.Join("；", parts);
                }
                sp.Children.Add(new TextBlock
                {
                    Text = "　" + line,
                    FontSize = 12.5,
                    TextWrapping = TextWrapping.Wrap,
                    Opacity = 0.85,
                });
            }
            card.Child = sp;
            FriendlyList.Children.Add(card);
            shown++;
        }
        if (evs == null || evs.Count == 0)
            FriendlyList.Children.Add(new TextBlock { Text = "这张地图还没有事件。", Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.Gray) });
    }



    static string Short(string p) => Path.GetFileName(p);

    async void BtnExport_Click(object sender, RoutedEventArgs e)
    {
        var stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        var outFile = Path.Combine(Fx.EventDir, $"map_events_{stamp}.json");
        try
        {
            var code = await Job.RunAsync("导出事件", Cmds.EventExport(outFile),
                line => Ui.Log(LogBox, line));
            ReloadFiles();
            if (code == 0)
            {
                Ui.Banner(ResultBar, "事件已导出！", $"文件保存在：{outFile}", true);
                if (File.Exists(outFile)) _ = ShowFile(outFile);
            }
            else
                Ui.Banner(ResultBar, "导出失败", "详见任务日志；大地图需要几分钟，请耐心等待。", false);
        }
        catch (Exception ex) { Ui.Banner(ResultBar, "导出失败", ex.Message, false); }
    }

    async void BtnRead_Click(object sender, RoutedEventArgs e)
    {
        var p = ImportFile.SelectedItem as string;
        if (p == null) { await Ui.Info(this, "提示", "没有可选文件，先导出"); return; }
        await ShowFile(p);
    }

    async void BtnPick_Click(object sender, RoutedEventArgs e)
    {
        var f = await Ui.PickFile(this, ".json");
        if (f == null) return;
        if (!ImportFile.Items.Contains(f.Path)) { ImportFile.Items.Add(f.Path); DslSrc.Items.Add(f.Path); }
        ImportFile.SelectedItem = f.Path;
        DslSrc.SelectedItem = f.Path;
        await ShowFile(f.Path);
    }

    async void BtnImport_Click(object sender, RoutedEventArgs e)
    {
        var p = ImportFile.SelectedItem as string;
        if (p == null) { await Ui.Info(this, "先选个文件", "点「选择事件文件…」挑一个事件文件（.json）。"); return; }
        bool apply = ImportApply.IsChecked == true;
        if (!apply)
            Ui.Banner(ResultBar, "预览运行中…", "正在计算导入计划，不会改动地图。", true);
        var code = -1;
        try
        {
            code = await Job.RunAsync("导入事件",
                Cmds.EventImport(p, apply, ImportPrune.IsChecked == true, TolBox.Text.Trim()),
                line => Ui.Log(LogBox, line));
        }
        catch (Exception ex) { Ui.Log(LogBox, "[错误] " + ex.Message); }
        if (!apply)
        {
            if (code == 0)
                Ui.Banner(ResultBar, "预览完成（没有改动地图）", "看上方日志里的计划；确认没问题就勾选「真正导入」再来一次。", true);
            else
                Ui.Banner(ResultBar, "预览失败", "详见任务日志。", false);
            return;
        }
        if (code == 0)
        {
            Ui.Banner(ResultBar, "导入完成！", "事件已写入地图，记得保存。", true);
            if (await Ui.SaveReminder(this))
            {
                try
                {
                    var r = await Fx.SaveMapFlow();
                    Ui.Log(LogBox, r["ok"]?.GetValue<bool>() == true ? "[完成] 服务端已回执保存" : "[失败] " + r.ToJsonString());
                    if (r["ok"]?.GetValue<bool>() == true)
                        Ui.Banner(ResultBar, "已保存", "地图已写入游戏存档。", true);
                }
                catch (Exception ex) { Ui.Banner(ResultBar, "保存失败", ex.Message, false); }
            }
        }
        else
            Ui.Banner(ResultBar, "导入失败/中断", "详见任务日志。", false);
    }

    async void BtnDslTo_Click(object sender, RoutedEventArgs e)
    {
        var p = DslSrc.SelectedItem as string;
        if (p == null) { await Ui.Info(this, "提示", "先选择机器文件"); return; }
        var dst = Path.Combine(Path.GetDirectoryName(p), DslDst.Text.Trim());
        await Ui.RunCli(this, LogBox, "to-dsl", Cmds.EventDsl("to-dsl", p, dst));
    }

    async void BtnDslFrom_Click(object sender, RoutedEventArgs e)
    {
        var p = DslSrc.SelectedItem as string;
        if (p == null) { await Ui.Info(this, "提示", "先选择机器文件"); return; }
        var dst = Path.Combine(Path.GetDirectoryName(p), DslDst.Text.Trim());
        await Ui.RunCli(this, LogBox, "from-dsl", Cmds.EventDsl("from-dsl", dst, p));
    }

    void BtnLabelsDump_Click(object sender, RoutedEventArgs e) =>
        _ = Ui.RunCli(this, LogBox, "生成标签", Cmds.LabelsDump());

    void BtnLabelsCheck_Click(object sender, RoutedEventArgs e) =>
        _ = Ui.RunCli(this, LogBox, "校验标签", Cmds.LabelsCheck());
}
