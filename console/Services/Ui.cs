using FuxiangConsole.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Windows.Storage;

namespace FuxiangConsole.Services;

public static class Ui
{
    public static string Pretty(JsonNode n) =>
        n?.ToJsonString(new JsonSerializerOptions
        {
            WriteIndented = true,
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        }) ?? "null";

    public static async Task<bool> Confirm(Page p, string title, string msg)
    {
        var d = new ContentDialog
        {
            Title = title,
            Content = msg,
            PrimaryButtonText = "确定",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
            XamlRoot = p.XamlRoot,
        };
        return await d.ShowAsync() == ContentDialogResult.Primary;
    }

    /// <summary>会改动地图的操作完成后提醒保存；返回 true 表示用户选了「现在保存」。</summary>
    public static async Task<bool> SaveReminder(Page p)
    {
        var sp = new StackPanel { Spacing = 10 };
        sp.Children.Add(new TextBlock
        {
            Text = "刚才的操作改动了地图内容，但还没有写入游戏存档。\n" +
                   "现在保存一次吗？（也可以稍后在游戏里自己点保存）",
            TextWrapping = TextWrapping.Wrap,
        });
        var d = new ContentDialog
        {
            Title = "别忘了保存地图",
            Content = sp,
            PrimaryButtonText = "现在保存",
            CloseButtonText = "稍后再说",
            DefaultButton = ContentDialogButton.Primary,
            XamlRoot = p.XamlRoot,
        };
        return await d.ShowAsync() == ContentDialogResult.Primary;
    }

    /// <summary>在页面顶部横幅显示一句人话结果（成功绿 / 失败红）。</summary>
    public static void Banner(InfoBar bar, string title, string message, bool ok)
    {
        bar.Title = title;
        bar.Message = message ?? "";
        bar.Severity = ok ? InfoBarSeverity.Success : InfoBarSeverity.Error;
        bar.IsOpen = true;
    }

    /// <summary>任务日志里最后一行 [完成]/[失败] 之后的原始输出，用于提取结果。</summary>
    public static string TailAfter(string log, string marker)
    {
        if (string.IsNullOrEmpty(log)) return "";
        int i = log.LastIndexOf(marker, StringComparison.Ordinal);
        return i < 0 ? log : log[(i + marker.Length)..];
    }

    public static async Task Info(Page p, string title, object content)
    {
        var d = new ContentDialog
        {
            Title = title,
            Content = content,
            CloseButtonText = "关闭",
            XamlRoot = p.XamlRoot,
        };
        await d.ShowAsync();
    }

    // ---- 日志：任意线程可调；缓冲 + 定时批量刷写，避免 TextBox O(n²) 卡死 UI ----
    static readonly System.Collections.Concurrent.ConcurrentDictionary<TextBox, LogSink> Sinks = new();

    public static void Log(TextBox box, string line) => Sinks.GetOrAdd(box, b => new LogSink(b)).Add(line);

    /// <summary>立即把缓冲刷进日志框（任务结束时调用，保证随后能读到完整日志）。</summary>
    public static void FlushLog(TextBox box)
    {
        if (Sinks.TryGetValue(box, out var sink)) sink.FlushNow();
    }

    sealed class LogSink
    {
        readonly TextBox _box;
        readonly object _lock = new();
        readonly List<string> _pending = new();
        Microsoft.UI.Dispatching.DispatcherQueueTimer _timer;

        const int PendingCap = 4000;     // 缓冲上限（超出丢最旧的）
        const int TextCap = 150_000;     // TextBox 内容上限（字符）
        const int TextTail = 110_000;    // 超限后保留的尾部

        public LogSink(TextBox box) { _box = box; }

        public void Add(string line)
        {
            lock (_lock)
            {
                _pending.Add(line);
                if (_pending.Count > PendingCap)
                    _pending.RemoveRange(0, _pending.Count - PendingCap);
            }
            EnsureTimer();
        }

        void EnsureTimer()
        {
            if (_timer != null) return;
            _box.DispatcherQueue.TryEnqueue(() =>
            {
                if (_timer != null) return;
                _timer = _box.DispatcherQueue.CreateTimer();
                _timer.Interval = TimeSpan.FromMilliseconds(300);
                _timer.Tick += (_, _) => Flush();
                _timer.Start();
            });
        }

        void Flush()
        {
            string[] batch;
            lock (_lock)
            {
                if (_pending.Count == 0) return;
                batch = _pending.ToArray();
                _pending.Clear();
            }
            Append(batch);
        }

        /// <summary>必须在 UI 线程调用。</summary>
        internal void FlushNow()
        {
            string[] batch;
            lock (_lock)
            {
                if (_pending.Count == 0) return;
                batch = _pending.ToArray();
                _pending.Clear();
            }
            Append(batch);
        }

        void Append(string[] batch)
        {
            var cur = _box.Text ?? "";
            if (cur.Length > TextCap)
                cur = "…（更早日志已截断）…\n" + cur[^TextTail..];
            _box.Text = cur + string.Join(Environment.NewLine, batch) + Environment.NewLine;
            var scroll = FindDescendantScroll(_box);
            scroll?.ChangeView(null, double.MaxValue, null, true);
        }
    }

    static ScrollViewer FindDescendantScroll(DependencyObject d)
    {
        int n = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChildrenCount(d);
        for (int i = 0; i < n; i++)
        {
            var c = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChild(d, i);
            if (c is ScrollViewer sv) return sv;
            var r = FindDescendantScroll(c);
            if (r != null) return r;
        }
        return null;
    }

    /// <summary>沿可视树向上找所属的 Expander（用于出问题时自动展开技术详情）。</summary>
    public static Expander FindAncestorExpander(DependencyObject d)
    {
        while (d != null)
        {
            if (d is Expander e) return e;
            d = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetParent(d);
        }
        return null;
    }

    /// <summary>跑一个 CLI 长任务：写日志、显示退出码；同时只允许一个。onLine 任意线程回调。
    /// 返回退出码（跑不起来/抛异常记 -1），调用方可以据此分开成功与失败的横幅。
    /// 返回前会把日志缓冲完整落盘，调用方随后可读 LogBox.Text。</summary>
    public static async Task<int> RunCli(Page page, TextBox log, string label, IReadOnlyList<string> argv, Action onDone = null)
    {
        Log(log, $"$ {string.Join(" ", argv.Select(a => a.Contains(' ') ? "\"" + a + "\"" : a))}");
        int code = -1;
        try
        {
            code = await Job.RunAsync(label, argv, line => Log(log, line));
            Log(log, code == 0 ? $"[完成] {label}（exit 0）" : $"[失败] {label}（exit {code}）");
        }
        catch (Exception ex)
        {
            Log(log, $"[错误] {ex.Message}");
        }
        FlushLog(log);   // 延续在 UI 线程上，直接落盘
        onDone?.Invoke();
        return code;
    }

    public sealed class FileRow
    {
        public string Name; public string Path; public long Size; public DateTime Mtime;
        public override string ToString() => Name;
    }

    public static List<FileRow> ListFiles(string dir, string ext = null)
    {
        var list = new List<FileRow>();
        try
        {
            if (!Directory.Exists(dir)) return list;
            foreach (var f in Directory.GetFiles(dir))
            {
                if (ext != null && !f.EndsWith(ext, StringComparison.OrdinalIgnoreCase)) continue;
                var fi = new FileInfo(f);
                list.Add(new FileRow { Name = fi.Name, Path = f, Size = fi.Length, Mtime = fi.LastWriteTime });
            }
            list.Sort((a, b) => b.Mtime.CompareTo(a.Mtime));
        }
        catch { }
        return list;
    }

    public static string FmtSize(long n) =>
        n < 1024 ? $"{n} B" : n < 1048576 ? $"{n / 1024.0:0.#} KB" : $"{n / 1048576.0:0.##} MB";

    /// <summary>FileOpenPicker（unpackaged 需要 HWND 初始化）。</summary>
    public static async Task<StorageFile> PickFile(Page p, params string[] exts)
    {
        var picker = new Windows.Storage.Pickers.FileOpenPicker();
        WinRT.Interop.InitializeWithWindow.Initialize(picker,
            WinRT.Interop.WindowNative.GetWindowHandle(App.Main));
        foreach (var e in exts) picker.FileTypeFilter.Add(e);
        return await picker.PickSingleFileAsync();
    }
}
