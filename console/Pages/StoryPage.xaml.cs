using FuxiangConsole.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using System.Text.Json.Nodes;

namespace FuxiangConsole.Pages;

public sealed partial class StoryPage : Page
{
    public StoryPage()
    {
        InitializeComponent();
        Loaded += (_, _) => { ReloadImportFiles(); _ = LoadTree(); };
    }

    void ReloadImportFiles()
    {
        var cur = (ImportFile.SelectedItem as string);
        ImportFile.Items.Clear();
        foreach (var f in Ui.ListFiles(Fx.StoryDir))
        {
            if (!f.Name.EndsWith(".json", StringComparison.OrdinalIgnoreCase) && !f.Name.EndsWith(".md", StringComparison.OrdinalIgnoreCase)) continue;
            ImportFile.Items.Add(f.Path);
        }
        int i = ImportFile.Items.IndexOf(cur);
        ImportFile.SelectedIndex = i >= 0 ? i : (ImportFile.Items.Count > 0 ? 0 : -1);
    }

    static string Short(string p) => Path.GetFileName(p);

    async Task LoadTree()
    {
        TreeRing.IsActive = true;
        try
        {
            var v = await Fx.CallOp("story_tree", new JsonObject());
            Tree.RootNodes.Clear();
            if (v is not JsonObject t) return;
            var stories = t["stories"] as JsonArray;
            int nc = 0, nd = 0;
            foreach (var s in stories ?? new JsonArray())
            {
                var so = s as JsonObject;
                var sNode = new TreeViewNode
                {
                    Content = MakeLabel($"剧情：{so?["name"] ?? "(未命名)"}　#{so?["ugc_id"]}"),
                };
                foreach (var d in so?["dialogues"] as JsonArray ?? new JsonArray())
                {
                    var dvo = d as JsonObject;
                    nd++;
                    var dNode = new TreeViewNode
                    {
                        Content = MakeLabel($"对话：{Trunc(dvo?["name"] ?? dvo?["content"])}　#{dvo?["ugc_id"]} sort={dvo?["sort_id"]}"),
                    };
                    var contentLine = MakeLabel(Trunc(dvo?["content"]) ?? "");
                    contentLine.Opacity = 0.7;
                    contentLine.FontSize = 11.5;
                    dNode.Children.Add(new TreeViewNode { Content = contentLine, IsExpanded = false });
                    foreach (var c in dvo?["choices"] as JsonArray ?? new JsonArray())
                    {
                        var cvo = c as JsonObject;
                        nc++;
                        dNode.Children.Add(new TreeViewNode
                        {
                            Content = MakeLabel($"选项：{Trunc(cvo?["name"] ?? cvo?["content"])}　#{cvo?["ugc_id"]}"),
                        });
                    }
                    sNode.Children.Add(dNode);
                }
                Tree.RootNodes.Add(sNode);
            }
            int orphD = (t["orphan_dialogues"] as JsonArray)?.Count ?? 0;
            int orphC = (t["orphan_choices"] as JsonArray)?.Count ?? 0;
            Tree.RootNodes.Add(new TreeViewNode
            {
                Content = MakeLabel($"孤儿对话 {orphD} · 孤儿选项 {orphC}"),
            });
        }
        catch (Exception ex) { Ui.Log(LogBox, "[错误] story_tree：" + ex.Message); }
        finally { TreeRing.IsActive = false; }
    }

    static TextBlock MakeLabel(string text) => new()
    {
        Text = text,
        FontSize = 12.5,
        VerticalAlignment = VerticalAlignment.Center,
    };

    static string Trunc(JsonNode n)
    {
        if (n == null) return "";
        var s = n.ToString().Replace("\n", " ");
        return s.Length > 46 ? s[..46] + "…" : s;
    }

    async void BtnTree_Click(object sender, RoutedEventArgs e) => await LoadTree();

    async Task Export(string fmt)
    {
        var stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        var ext = fmt == "md" ? ".md" : ".json";
        var outFile = Path.Combine(Fx.StoryDir, $"story_{stamp}{ext}");
        try
        {
            var code = await Job.RunAsync("导出剧情", Cmds.StoryExport(fmt, outFile),
                line => Ui.Log(LogBox, line));
            if (code == 0)
                Ui.Banner(ResultBar, "剧情已导出！", $"文件保存在：{outFile}", true);
            else
                Ui.Banner(ResultBar, "导出失败", "详见任务日志。", false);
        }
        catch (Exception ex) { Ui.Banner(ResultBar, "导出失败", ex.Message, false); }
        DispatcherQueue.TryEnqueue(ReloadImportFiles);
    }
    void BtnExportJson_Click(object sender, RoutedEventArgs e) => _ = Export("json");
    void BtnExportMd_Click(object sender, RoutedEventArgs e) => _ = Export("md");

    void BtnResume_Click(object sender, RoutedEventArgs e) =>
        _ = Ui.RunCli(this, LogBox, "续跑剧情导入", Cmds.StoryResume());

    async void BtnFields_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var v = await Fx.CallOp("story_fields", new JsonObject());
            await Ui.Info(this, "剧情字段参考", new ScrollViewer
            {
                MaxHeight = 420,
                Content = new TextBox
                {
                    IsReadOnly = true,
                    AcceptsReturn = true,
                    TextWrapping = TextWrapping.NoWrap,
                    FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Cascadia Mono"),
                    FontSize = 12,
                    Text = Ui.Pretty(v),
                    Width = 680,
                    Height = 400,
                },
            });
        }
        catch (Exception ex) { await Ui.Info(this, "失败", ex.Message); }
    }

    async void BtnPickImport_Click(object sender, RoutedEventArgs e)
    {
        var f = await Ui.PickFile(this, ".json", ".md");
        if (f == null) return;
        if (!ImportFile.Items.Contains(f.Path)) ImportFile.Items.Add(f.Path);
        ImportFile.SelectedItem = f.Path;
    }

    async void BtnImport_Click(object sender, RoutedEventArgs e)
    {
        var file = ImportFile.SelectedItem as string;
        if (string.IsNullOrEmpty(file)) { await Ui.Info(this, "先选个文件", "点「选择文件…」挑一个剧情文件（.json 或 .md）。"); return; }
        bool apply = ImportApply.IsChecked == true;
        if (!apply)
        {
            Ui.Banner(ResultBar, "预览运行中…", "正在计算导入计划，不会改动地图。看完计划再勾选「真正导入」。", true);
        }
        var code = -1;
        try
        {
            code = await Job.RunAsync("导入剧情",
                Cmds.StoryImport(file, apply, ImportPrune.IsChecked == true),
                line => Ui.Log(LogBox, line));
        }
        catch (Exception ex) { Ui.Log(LogBox, "[错误] " + ex.Message); }
        if (!apply)
        {
            if (code == 0)
                Ui.Banner(ResultBar, "预览完成（没有改动地图）", "看上方日志里的计划；确认没问题就勾选「真正导入」再点一次「开始导入」。", true);
            else
                Ui.Banner(ResultBar, "预览失败", "详见任务日志。", false);
            return;
        }
        if (code == 0)
        {
            Ui.Banner(ResultBar, "导入完成！", "剧情已写入地图，记得保存。", true);
            await AskSave();
        }
        else
        {
            Ui.Banner(ResultBar, "导入失败/中断", "详见任务日志；如果上次导入被打断，用「续跑被中断的导入」恢复。", false);
        }
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

    static string[] ParseFields(string text) =>
        text.Split('\n').Select(s => s.Trim()).Where(s => s.Length > 0).ToArray();

    async void BtnSet_Click(object sender, RoutedEventArgs e)
    {
        var id = SetId.Text.Trim();
        if (id.Length == 0) { await Ui.Info(this, "提示", "ugc_id 为空"); return; }
        await Ui.RunCli(this, LogBox, "story_set",
            Cmds.StorySet(id, ParseFields(SetFields.Text), SetDry.IsChecked == true));
    }

    async void BtnCreate_Click(object sender, RoutedEventArgs e)
    {
        var kind = ((ComboBoxItem)CreateKind.SelectedItem).Content as string;
        kind = kind.Split(' ')[0];
        await Ui.RunCli(this, LogBox, "story_create",
            Cmds.StoryCreate(kind, ParseFields(CreateFields.Text)));
    }

    async void BtnDelete_Click(object sender, RoutedEventArgs e)
    {
        var ids = DelIds.Text.Trim();
        if (ids.Length == 0) { await Ui.Info(this, "提示", "ugc_ids 为空"); return; }
        if (DelForce.IsChecked != true && DelDry.IsChecked != true)
        {
            var r = await Ui.Confirm(this, "未勾选 --force",
                "story_delete 删除非登记集合里的节点需要 --force。继续用 --force 删除？");
            if (!r) return;
            DelForce.IsChecked = true;
        }
        await Ui.RunCli(this, LogBox, "story_delete",
            Cmds.StoryDelete(ids, DelCascade.IsChecked == true, DelForce.IsChecked == true, DelDry.IsChecked == true));
    }
}
