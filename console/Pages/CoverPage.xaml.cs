using FuxiangConsole.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media.Imaging;
using System.Text.Json.Nodes;
using Windows.Storage;
using Windows.Storage.Pickers;
using Windows.Storage.Streams;

namespace FuxiangConsole.Pages;

public sealed partial class CoverPage : Page
{
    StorageFile _picked;

    public CoverPage()
    {
        InitializeComponent();
        Loaded += (_, _) => _ = LoadCoverState();
    }

    async Task LoadCoverState()
    {
        StateRing.IsActive = true;
        try
        {
            await Ui.RunCli(this, LogBox, "查看封面", Cmds.CoverState());
            var text = LogBox.Text ?? "";
            var m = System.Text.RegularExpressions.Regex.Match(text, "\\{[\\s\\S]*\\}");
            if (!m.Success) { StateView.Text = "（日志里没找到 JSON）"; ShowNoCover("读取封面状态失败，详见任务日志。"); return; }
            JsonNode node;
            try { node = JsonNode.Parse(m.Value); }
            catch (Exception pe) { StateView.Text = "解析失败：" + pe.Message + "\n" + m.Value; ShowNoCover("解析封面状态失败。"); return; }
            StateView.Text = Ui.Pretty(node);

            // 主封面显示优先级：本地主封面槽（slots[0].photo_path 相对 doc_dir）→ 服务器回读 URL → 无封面
            string localFile = null, url = null, source = null;
            var docDir = node["doc_dir"]?.ToString();
            var slots = node["slots"] as JsonArray;
            if (slots != null && slots.Count > 0 && slots[0] is JsonObject s0)
            {
                var rel = s0["photo_path"]?.ToString();
                if (!string.IsNullOrEmpty(rel) && !string.IsNullOrEmpty(docDir))
                {
                    var f = Path.Combine(docDir, rel.Replace('/', '\\'));
                    if (File.Exists(f)) { localFile = f; source = "本地主封面槽（尚未保存也会显示，保存并重进后其他人可见）"; }
                }
            }
            var cover = node["ugc_room_cover"]?.ToString();
            if (!string.IsNullOrEmpty(cover))
            {
                if (cover.StartsWith("http", StringComparison.OrdinalIgnoreCase))
                {
                    url = cover;
                    CoverUrlText.Text = "服务器回读封面 URL：" + cover;
                    CoverUrlText.Visibility = Visibility.Visible;
                    if (localFile == null) source = "服务器回读封面（已保存生效）";
                }
                else if (localFile == null && !string.IsNullOrEmpty(docDir))
                {
                    // ugc_room_cover 有值但不是 URL 时也尝试按相对路径找本地文件
                    var f = Path.Combine(docDir, cover.Replace('/', '\\'));
                    if (File.Exists(f)) { localFile = f; source = "本地文件（来自服务器回读）"; }
                }
            }

            if (localFile != null)
            {
                CoverNow.Source = new BitmapImage(new Uri(localFile));
                CoverNow.Visibility = Visibility.Visible;
                CoverNone.Visibility = Visibility.Collapsed;
                CoverCaption.Text = source;
            }
            else if (url != null)
            {
                CoverNow.Source = new BitmapImage(new Uri(url));
                CoverNow.Visibility = Visibility.Visible;
                CoverNone.Visibility = Visibility.Collapsed;
                CoverCaption.Text = source;
            }
            else
            {
                ShowNoCover("这张地图还没有设置封面。");
            }
        }
        catch (Exception ex) { ShowNoCover("读取封面失败：" + ex.Message); }
        finally { StateRing.IsActive = false; }
    }

    void ShowNoCover(string msg)
    {
        CoverNow.Visibility = Visibility.Collapsed;
        CoverNone.Text = msg + "\n在右边选一张图片上传即可。";
        CoverNone.Visibility = Visibility.Visible;
        CoverCaption.Text = "";
    }

    async void BtnState_Click(object sender, RoutedEventArgs e) => await LoadCoverState();

    async void DropZone_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e)
    {
        var f = await Ui.PickFile(this, ".jpg", ".jpeg", ".png", ".webp", ".bmp");
        if (f == null) return;
        _picked = f;
        using var stream = await f.OpenReadAsync();
        var bmp = new BitmapImage();
        await bmp.SetSourceAsync(stream);
        Preview.Source = bmp;
        Preview.Visibility = Visibility.Visible;
        var size = stream.Size;
        FileInfo.Text = $"{f.Name} · {Ui.FmtSize((long)size)} · {f.Path}";
        BtnUpload.IsEnabled = true;
    }

    async void BtnUpload_Click(object sender, RoutedEventArgs e)
    {
        if (_picked == null) return;
        if (!await Ui.Confirm(this, "上传封面",
            "将把这张图片设为地图封面（自动裁成 16:9 并压缩）。只改封面，不会发布地图。继续？"))
            return;
        UpRing.IsActive = true;
        try
        {
            var code = await Job.RunAsync("封面上传", Cmds.CoverFromFile(_picked.Path, "0"),
                line => Ui.Log(LogBox, line));
            if (code == 0)
            {
                Ui.Banner(ResultBar, "封面已应用！", "上方已显示新封面。保存并重进地图后其他人也能看到。", true);
                await LoadCoverState();
            }
            else
                Ui.Banner(ResultBar, "上传失败", "详见任务日志；检查图片格式（jpg/png，不超过 12MB）。", false);
        }
        catch (Exception ex) { Ui.Banner(ResultBar, "上传失败", ex.Message, false); }
        finally { UpRing.IsActive = false; }
    }
}
