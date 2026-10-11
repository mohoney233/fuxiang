using FuxiangConsole.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media.Imaging;

namespace FuxiangConsole.Pages;

public sealed partial class ShotPage : Page
{
    List<Ui.FileRow> _files = new();

    public ShotPage()
    {
        InitializeComponent();
        Loaded += (_, _) => Reload();
    }

    void BtnReload_Click(object sender, RoutedEventArgs e) => Reload();

    void Reload()
    {
        _files = Ui.ListFiles(Fx.ShotsDir, ".png");
        ShotList.Items.Clear();
        foreach (var f in _files.Take(60))
            ShotList.Items.Add(new TextBlock
            {
                Text = $"{f.Mtime:MM-dd HH:mm}  {f.Name}",
                FontSize = 12,
                TextTrimming = TextTrimming.CharacterEllipsis,
            });
    }

    void ShotList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        int i = ShotList.SelectedIndex;
        if (i < 0 || i >= _files.Count) return;
        var f = _files[i];
        BigImage.Source = new BitmapImage(new Uri(f.Path));
        BigCaption.Text = $"{f.Name} · {Ui.FmtSize(f.Size)} · {f.Mtime:yyyy-MM-dd HH:mm:ss}";
    }

    async Task Shot(string label, List<string> argv)
    {
        await Ui.RunCli(this, LogBox, label, argv);
        DispatcherQueue.TryEnqueue(() =>
        {
            // fxshot 单视角/平面图把产物写在 preview\ 固定名（会被下次覆盖），
            // 只有 sweep 才进图库目录 preview\shots\。这里把产物拷一份进图库再刷新。
            var text = LogBox.Text ?? "";
            var tail = text.Length > 4000 ? text[^4000..] : text;
            var m = System.Text.RegularExpressions.Regex.Match(tail, @"""path"":\s*""([^""]+\.png)""");
            string showPath = null;
            if (m.Success)
            {
                var p = m.Groups[1].Value.Replace("\\\\", "\\");
                if (System.IO.File.Exists(p))
                {
                    showPath = p;
                    if (!p.StartsWith(Fx.ShotsDir, StringComparison.OrdinalIgnoreCase))
                    {
                        try
                        {
                            var sub = argv.Count > 3 ? argv[3] : "shot";
                            var view = argv.Count > 4 ? argv[4] : null;
                            var name = view != null ? $"{sub}_{view}" : sub;
                            var dst = Path.Combine(Fx.ShotsDir, $"{name}_{DateTime.Now:yyyyMMdd_HHmmss}.png");
                            System.IO.File.Copy(p, dst, true);
                            showPath = dst;
                        }
                        catch { /* 拷贝失败则预览原路径 */ }
                    }
                }
            }
            Reload();
            if (showPath != null && System.IO.File.Exists(showPath))
            {
                BigImage.Source = new BitmapImage(new Uri(showPath));
                var fi = new System.IO.FileInfo(showPath);
                BigCaption.Text = $"{fi.Name} · {Ui.FmtSize(fi.Length)} · {fi.LastWriteTime:yyyy-MM-dd HH:mm:ss}";
            }
        });
    }

    void BtnTop_Click(object sender, RoutedEventArgs e) => _ = Shot("俯视截图", Cmds.ShotShot("top"));
    void BtnSweep_Click(object sender, RoutedEventArgs e) => _ = Shot("全套截图", Cmds.ShotSweep());
    void BtnLayout_Click(object sender, RoutedEventArgs e) => _ = Shot("平面图", Cmds.ShotLayout());
    void BtnRestore_Click(object sender, RoutedEventArgs e) => _ = Shot("还原相机", Cmds.ShotRestore());
}
