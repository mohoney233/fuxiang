using FuxiangConsole.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;

namespace FuxiangConsole.Pages;

public sealed partial class SnapshotPage : Page
{
    List<Ui.FileRow> _files = new();

    public SnapshotPage()
    {
        InitializeComponent();
        Loaded += (_, _) => ReloadFiles();
    }

    void BtnReload_Click(object sender, RoutedEventArgs e) => ReloadFiles();

    void ReloadFiles()
    {
        _files = Ui.ListFiles(Fx.SnapshotsDir, ".json");
        FileList.Items.Clear();
        foreach (var f in _files)
            FileList.Items.Add($"{f.Name}   ({Ui.FmtSize(f.Size)}, {f.Mtime:MM-dd HH:mm})");
        RfResumeFrom.Items.Clear();
        RfResumeFrom.Items.Add("（不使用）");
        foreach (var f in _files) RfResumeFrom.Items.Add(f.Name);
        RfResumeFrom.SelectedIndex = 0;
    }

    Ui.FileRow Selected() =>
        FileList.SelectedIndex >= 0 && FileList.SelectedIndex < _files.Count ? _files[FileList.SelectedIndex] : null;

    async void BtnExport_Click(object sender, RoutedEventArgs e)
    {
        ExportRing.IsActive = true;
        var stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        var outFile = Path.Combine(Fx.SnapshotsDir, $"backup_{stamp}.json");
        try
        {
            var code = await Job.RunAsync("备份地图", Cmds.SnapshotExport(outFile, null),
                line => Ui.Log(LogBox, line));
            if (code == 0)
                Ui.Banner(ExportBar, "备份成功！",
                    $"备份文件已保存：{outFile}（{Ui.FmtSize(new System.IO.FileInfo(outFile).Length)}）", true);
            else
                Ui.Banner(ExportBar, "备份失败", "详见下方任务日志；常见原因是游戏或编辑器没开。", false);
        }
        catch (Exception ex) { Ui.Banner(ExportBar, "备份失败", ex.Message, false); }
        ExportRing.IsActive = false;
        ReloadFiles();
    }

    async void BtnInspect_Click(object sender, RoutedEventArgs e)
    {
        var f = Selected();
        if (f == null) { await Ui.Info(this, "提示", "先选中一份快照"); return; }
        await Ui.RunCli(this, LogBox, "inspect", Cmds.SnapshotInspect(f.Path));
    }

    async void BtnCheck_Click(object sender, RoutedEventArgs e)
    {
        var f = Selected();
        if (f == null) { await Ui.Info(this, "提示", "先选中一份快照"); return; }
        await Ui.RunCli(this, LogBox, "check", Cmds.SnapshotCheck(f.Path));
    }

    async void BtnDiff_Click(object sender, RoutedEventArgs e)
    {
        var f = Selected();
        if (f == null) { await Ui.Info(this, "提示", "先选中一份快照作为 A"); return; }
        var combo = new ComboBox { MinWidth = 360 };
        foreach (var g in _files.Where(x => x.Path != f.Path)) combo.Items.Add(g.Name);
        if (combo.Items.Count == 0) { await Ui.Info(this, "提示", "没有可对比的另一份快照"); return; }
        combo.SelectedIndex = 0;
        var chk = new CheckBox { Content = "--include-identity（地图名/简介/标签也比对）" };
        var panel = new StackPanel { Spacing = 10 };
        panel.Children.Add(new TextBlock { Text = "选择 B 侧快照：", TextWrapping = TextWrapping.Wrap });
        panel.Children.Add(combo);
        panel.Children.Add(chk);
        var d = new ContentDialog
        {
            Title = "对比 " + f.Name,
            Content = panel,
            PrimaryButtonText = "对比",
            CloseButtonText = "取消",
            XamlRoot = XamlRoot,
        };
        if (await d.ShowAsync() != ContentDialogResult.Primary) return;
        var b = _files.FirstOrDefault(x => x.Name == (string)combo.SelectedItem);
        if (b == null) return;
        await Ui.RunCli(this, LogBox, "diff", Cmds.SnapshotDiff(f.Path, b.Path, null, chk.IsChecked == true));
    }

    void BtnRestoreOpen_Click(object sender, RoutedEventArgs e)
    {
        var f = Selected();
        if (f == null) { _ = Ui.Info(this, "提示", "先选中一份快照"); return; }
        RestoreCard.Visibility = Visibility.Visible;
        RestoreFile.Text = f.Name;
        RestoreCard.Tag = f.Path;
    }

    Cmds.RestoreOpt Collect(bool apply)
    {
        var file = (string)RestoreCard.Tag;
        string resume = null;
        if (RfResumeFrom.SelectedIndex > 0)
            resume = Path.Combine(Fx.SnapshotsDir, (string)RfResumeFrom.SelectedItem);
        return new Cmds.RestoreOpt
        {
            File = file,
            Apply = apply,
            WithStory = RfStory.IsChecked == true,
            IncludeUi = RfUi.IsChecked == true,
            IncludeMotion = RfMotion.IsChecked == true,
            IncludeGlobal = RfGlobal.IsChecked == true,
            SyncSettings = RfSync.IsChecked == true,
            NoProps = RfNoProps.IsChecked == true,
            NoEvents = RfNoEvents.IsChecked == true,
            NewRun = RfNewRun.IsChecked == true,
            AllowSentinel = RfSentinel.IsChecked == true,
            AllowSameMap = RfSameMap.IsChecked == true,
            AllowDanglingRefs = RfDangling.IsChecked == true,
            Limit = RfLimit.Text.Trim(),
            Batch = RfBatch.Text.Trim(),
            ResumeFrom = resume,
        };
    }

    async void BtnDry_Click(object sender, RoutedEventArgs e)
    {
        Ui.Banner(RestoreBar, "预览运行中…", "恢复是一个较长过程，完成后下方会显示计划内容（不会改动地图）。", true);
        var code = await Ui.RunCli(this, LogBox, "恢复预览", Cmds.SnapshotRestore(Collect(false)));
        if (code == 0)
            Ui.Banner(RestoreBar, "预览完成（没有改动地图）", "上面的日志是「将要做什么」的计划，确认没问题就点第 2 步。", true);
        else
            Ui.Banner(RestoreBar, "预览没跑成（也没改动地图）",
                "日志里有原因。常见情况：这份快照不是当前这张图（要勾「高级：允许同图恢复」或换一份备份）、" +
                "或缺某个 --allow-* 闸门。修正前不要点第 2 步。", false);
    }

    async void BtnApply_Click(object sender, RoutedEventArgs e)
    {
        if (ConfirmChk.IsChecked != true)
        {
            await Ui.Info(this, "先确认一下", "请先勾选「我确认要改动当前地图」，再开始恢复。");
            return;
        }
        if (!await Ui.Confirm(this, "开始恢复",
            "恢复会把备份里的物件、属性、事件放回当前地图。\n这是一个较长过程，期间请不要在游戏里操作编辑器。确定开始？"))
            return;
        Ui.Banner(RestoreBar, "恢复运行中…", "正在逐件恢复，地图大的话需要几分钟到几十分钟，期间请勿操作游戏编辑器。", true);
        var code = -1;
        try
        {
            code = await Job.RunAsync("恢复备份", Cmds.SnapshotRestore(Collect(true)),
                line => Ui.Log(LogBox, line));
        }
        catch (Exception ex) { Ui.Log(LogBox, "[错误] " + ex.Message); }
        if (code == 0)
        {
            Ui.Banner(RestoreBar, "恢复完成！", "建议现在保存一次地图，然后退出重进核对效果。", true);
            if (await Ui.SaveReminder(this))
            {
                try
                {
                    var r = await Fx.SaveMapFlow();
                    Ui.Log(LogBox, r["ok"]?.GetValue<bool>() == true ? "[完成] 服务端已回执保存" : "[失败] " + r.ToJsonString());
                    if (r["ok"]?.GetValue<bool>() == true)
                        Ui.Banner(RestoreBar, "恢复并保存完成", "建议退出重进核对一次效果。", true);
                }
                catch (Exception ex) { Ui.Banner(RestoreBar, "保存失败", ex.Message, false); }
            }
        }
        else
        {
            Ui.Banner(RestoreBar, "恢复中断/失败",
                "详见任务日志。继续恢复：重进地图后，在「恢复这份备份」里勾选「从上次中断处继续」再跑一次。", false);
        }
    }
}
