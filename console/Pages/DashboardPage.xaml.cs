using FuxiangConsole.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using System.Text.Json.Nodes;
using Windows.UI;

namespace FuxiangConsole.Pages;

public sealed partial class DashboardPage : Page
{
    DispatcherTimer _timer;
    int _tick;
    bool _ready;          // 编辑器 + 会话都可用
    bool _bridgeAlive;
    bool _dwrg;

    public DashboardPage()
    {
        InitializeComponent();
        Loaded += (_, _) =>
        {
            ShowFirstRunGuide();
            FillBridgeSelector();
            _ = RefreshAll();
            _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
            _timer.Tick += async (_, _) => await OnTick();
            _timer.Start();
        };
        Unloaded += (_, _) => _timer?.Stop();
    }

    // ---- 桥接实例切换 ----
    void FillBridgeSelector()
    {
        var all = Bridges.All();
        var want = Fx.Sel?.Name;
        BridgeSelector.Items.Clear();
        foreach (var inst in all)
        {
            var st = ReadInstanceState(inst);
            var tag = st.Alive ? "● 在线" : "○ 未连接";
            BridgeSelector.Items.Add($"{inst.Name}　{tag}" + (st.Pid != null ? $"　pid={st.Pid}" : ""));
        }
        int idx = all.FindIndex(i => i.Name == want);
        BridgeSelector.SelectedIndex = idx >= 0 ? idx : 0;
    }

    static Fx.BridgeState ReadInstanceState(Fx.BridgeInstance inst)
    {
        var st = new Fx.BridgeState();
        try
        {
            if (!File.Exists(inst.StatePath)) return st;
            st.Raw = File.ReadAllText(inst.StatePath);
            var n = JsonNode.Parse(st.Raw) as JsonObject;
            if (n == null) return st;
            st.Pid = n["pid"] is JsonValue pv && pv.TryGetValue<double>(out var d) ? (long)d : null;
            if (n["now"] is JsonValue nv && nv.TryGetValue<double>(out var now))
            {
                st.Age = Math.Round(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - now, 1);
                st.Alive = st.Age <= 10.0;
            }
        }
        catch { }
        return st;
    }

    /// <summary>已被桥接占用的游戏进程 pid：注册表记的 + 各通道心跳里的（主桥接的 pid 只在心跳里）。</summary>
    static HashSet<long> UsedPids(string exceptName = null)
    {
        var used = new HashSet<long>();
        foreach (var b in Bridges.All())
        {
            if (exceptName != null && b.Name == exceptName) continue;
            if (b.Pid is long p) used.Add(p);
            if (ReadInstanceState(b).Pid is long sp) used.Add(sp);
        }
        return used;
    }

    void BridgeSelector_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        var all = Bridges.All();
        int i = BridgeSelector.SelectedIndex;
        if (i < 0 || i >= all.Count) return;
        if (Fx.Sel == null || Fx.Sel.Name != all[i].Name)
            Fx.SetSelected(all[i]);
        _ = RefreshAll();
    }

    async void BtnAddBridge_Click(object sender, RoutedEventArgs e)
    {
        var pids = Bridges.DwrgPids();
        if (pids.Count == 0)
        {
            await Ui.Info(this, "没有找到游戏", "没有检测到运行中的 dwrg.exe（游戏）。\n多开桥接需要同时运行多个游戏客户端，每个客户端都进入编辑器后分别连接。");
            return;
        }
        var used = UsedPids();
        var pidBox = new ComboBox { MinWidth = 300, Header = "选择要连接的游戏窗口（pid）" };
        foreach (var (pid, mem) in pids)
        {
            var taken = used.Contains(pid);
            pidBox.Items.Add($"pid={pid}　内存 {mem}" + (taken ? "　（已有桥接）" : ""));
        }
        // 默认停在第一个没被占用的窗口（占用中的选了一也会被拦，还可能误顶掉现有连接）
        int firstFree = pids.FindIndex(p => !used.Contains(p.pid));
        pidBox.SelectedIndex = firstFree >= 0 ? firstFree : 0;
        var nameBox = new TextBox { Header = "给这个桥接起个名字", Text = $"桥接{Bridges.All().Count + 1}" };
        var panel = new StackPanel { Spacing = 12 };
        panel.Children.Add(new TextBlock
        {
            Text = "为另一个游戏窗口建立独立连接：会在控制台目录下创建一条专属通道，并把桥接注入你选的那个游戏。",
            TextWrapping = TextWrapping.Wrap,
        });
        panel.Children.Add(pidBox);
        panel.Children.Add(nameBox);
        var d = new ContentDialog
        {
            Title = "添加桥接",
            Content = panel,
            PrimaryButtonText = "创建并连接",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Primary,
            XamlRoot = XamlRoot,
        };
        if (await d.ShowAsync() != ContentDialogResult.Primary) return;

        var chosen = pids[Math.Max(0, pidBox.SelectedIndex)];
        if (used.Contains(chosen.pid))
        {
            await Ui.Info(this, "这个窗口已经有桥接了",
                $"pid={chosen.pid} 已连接到现有桥接，不能重复添加（重复注入会把原连接顶掉）。\n" +
                "如果要重连它，用「当前桥接」下拉框选中后点横幅里的「重新连接」。");
            return;
        }
        var name = string.IsNullOrWhiteSpace(nameBox.Text) ? $"桥接{Bridges.All().Count + 1}" : nameBox.Text.Trim();
        if (Bridges.All().Any(b => b.Name == name)) { await Ui.Info(this, "名字重复", "换个名字再来。"); return; }
        HeroRing.IsActive = true;
        try
        {
            var inst = Bridges.CreateSlot(name, chosen.pid);
            Bridges.WriteInstallOverride(inst);
            var code = await Job.RunAsync("安装桥接 " + name, Cmds.BridgeInstall(),
                line => Ui.Log(LogBox, line));
            Bridges.ClearInstallOverride();
            if (code == 0)
            {
                Bridges.UpdatePid(name, chosen.pid);
                Fx.SetSelected(Bridges.All().First(b => b.Name == name));
                FillBridgeSelector();
                Ui.Banner(ResultBar, $"桥接「{name}」已创建并连接",
                    "现在所有页面操作的都是这个桥接。要在多个窗口间切换，用上方「当前桥接」下拉框。", true);
                await RefreshAll();
            }
            else
            {
                Ui.Banner(ResultBar, "安装失败", "详见任务日志（常见原因：目标窗口还没进编辑器、权限不足）。", false);
            }
        }
        catch (Exception ex) { Ui.Banner(ResultBar, "添加失败", ex.Message, false); }
        finally { HeroRing.IsActive = false; }
    }

    async void BtnRemoveBridge_Click(object sender, RoutedEventArgs e)
    {
        var sel = Fx.Sel;
        if (sel == null || sel.IsMain) { await Ui.Info(this, "提示", "主桥接不能移除。"); return; }
        if (!await Ui.Confirm(this, "移除桥接 " + sel.Name,
            "只从控制台的列表里移除这条桥接（游戏里的桥不会被卸载，重启游戏后自然消失）。继续？"))
            return;
        Bridges.Remove(sel.Name);
        Fx.SetSelected(Bridges.All()[0]);
        FillBridgeSelector();
        await RefreshAll();
    }

    void ShowFirstRunGuide()
    {
        var s = Settings.Load();
        if (!s.FirstRunDone)
        {
            GuideBar.IsOpen = true;
            s.FirstRunDone = true;
            s.Save();
        }
    }

    void GuideBar_CloseClick(InfoBar sender, object args)
    {
        var s = Settings.Load();
        s.FirstRunDone = true;
        s.Save();
    }

    async Task OnTick()
    {
        _tick++;
        await RefreshHero();
        if (_tick % 5 == 0 && _ready) await LoadMapInfo();
    }

    async Task RefreshHero()
    {
        var st = Fx.ReadBridgeState();
        _bridgeAlive = st.Alive;
        _dwrg = await Fx.DwrgRunningAsync();
        var sel = Fx.Sel ?? Bridges.All()[0];
        BridgeStatusText.Text = st.Alive ? $"在线（心跳 {st.Age:0.#}s，pid {st.Pid}）"
                              : (st.Raw == null ? "从未连接过" : "掉线（心跳超过 10 秒）");
        BrText.Text = $"通道：{sel.Dir}" +
                      (st.Raw == null ? "\n状态：从未连接过（需要装桥）"
            : st.Alive ? $"\n状态：在线（pid {st.Pid}，启动于 {DateTimeOffset.FromUnixTimeSeconds((long)st.StartedAt).LocalDateTime:HH:mm:ss}）"
            : "\n状态：掉线（心跳超过 10 秒）");
        GameText.Text = "游戏进程：" + (_dwrg ? "运行中" : "未运行");

        if (!_dwrg)
        {
            SetHero("第一步：打开游戏",
                "启动《第五人格》，在游戏里进入「随笔浮想」编辑器并打开一张地图，然后回到这里。",
                InfoBarSeverity.Informational, null, false);
            EdText.Text = ""; SessText.Text = "";
        }
        else if (!_bridgeAlive)
        {
            SetHero($"第二步：连接「{sel.Name}」",
                "游戏在运行，但这条桥接还没有连上。点下面的按钮重新连接——会弹出授权窗口，请点「是」。",
                InfoBarSeverity.Warning, "重新连接", true);
            EdText.Text = ""; SessText.Text = "";
        }
        else
        {
            // 桥在线：看编辑器 / 会话
            var (core, sessConn, sessWarn) = await FetchEditorState();
            if (!core)
            {
                SetHero("还差一步：打开地图编辑器",
                    "控制台已连接，但游戏里还没打开「随笔浮想」的地图编辑器。请在游戏里进入编辑器并打开一张地图。",
                    InfoBarSeverity.Warning, null, false);
            }
            else if (!sessConn)
            {
                SetHero("游戏连接已断开",
                    sessWarn ?? "游戏与服务器的连接断了，保存等操作暂时不可用。请在游戏里退出并重新进入地图编辑器。",
                    InfoBarSeverity.Warning, null, false);
            }
            else
            {
                _ready = true;
                SetHero("一切就绪",
                    "左侧功能都可以正常使用了。改动地图后记得用上面的「保存地图」按钮保存。",
                    InfoBarSeverity.Success, null, false);
            }
        }
    }

    void SetHero(string title, string body, InfoBarSeverity sev, string buttonText, bool showRing)
    {
        HeroTitle.Text = title;
        HeroBody.Text = body;
        SolidColorBrush accent = sev switch
        {
            InfoBarSeverity.Success => new SolidColorBrush(Color.FromArgb(255, 63, 185, 80)),
            InfoBarSeverity.Warning => new SolidColorBrush(Color.FromArgb(255, 210, 153, 34)),
            InfoBarSeverity.Error => new SolidColorBrush(Color.FromArgb(255, 248, 81, 73)),
            _ => new SolidColorBrush(Color.FromArgb(255, 79, 143, 247)),
        };
        Hero.BorderBrush = accent;
        Hero.Background = new SolidColorBrush(Color.FromArgb(28, accent.Color.R, accent.Color.G, accent.Color.B));
        HeroTitle.Foreground = accent;
        HeroBtn.Visibility = buttonText == null ? Visibility.Collapsed : Visibility.Visible;
        if (buttonText != null) HeroBtn.Content = buttonText;
        HeroRing.Visibility = showRing ? Visibility.Visible : Visibility.Collapsed;
        HeroRing.IsActive = showRing;
    }

    async Task<(bool core, bool sessConn, string sessWarn)> FetchEditorState()
    {
        try
        {
            var st = await Fx.CallOp("status") as JsonObject;
            if (st == null) return (false, true, null);
            bool core = st["editor_core"] != null;
            bool cm = st["component_manager"]?.GetValue<bool>() ?? false;
            bool em = st["event_manager"]?.GetValue<bool>() ?? false;
            var fr = st["frame_rate"];
            EdText.Text = $"编辑器核心：{(core ? "可用" : "不可用")}　组件管理器：{(cm ? "可用" : "不可用")}　事件管理器：{(em ? "可用" : "不可用")}" +
                          (fr != null ? $"　帧率：{fr} fps" : "");
            var sess = st["server_session"] as JsonObject;
            bool conn = sess?["connected"]?.GetValue<bool>() ?? false;
            SessText.Text = "游戏服务器会话：" + (conn ? "已连接" : "已断开");
            return (core, conn, sess?["warning"]?.ToString());
        }
        catch (Exception ex)
        {
            EdText.Text = "状态读取失败：" + ex.Message;
            return (false, true, null);
        }
    }

    async Task RefreshAll()
    {
        await RefreshHero();
        await LoadMapInfo();
    }

    async Task LoadMapInfo()
    {
        if (!_bridgeAlive) { MapText.Text = "还没有读取到地图信息。"; return; }
        try
        {
            var v = await Fx.CallOp("map_info") as JsonObject;
            if (v == null || (v["map_setting"] as JsonObject) == null)
            { MapText.Text = "还没有读取到地图信息。"; return; }
            var ms = v["map_setting"] as JsonObject;
            int total = 0;
            if (v["type_distribution"] is JsonObject td)
                foreach (var kv in td) total += (kv.Value as JsonValue)?.TryGetValue<double>(out var d) == true ? (int)d : 0;
            var lines = new List<string>();
            lines.Add($"名称：{(ms["name"] != null ? ms["name"].ToString() : "（未命名）")}");
            if (ms["desc"] != null && ms["desc"].ToString().Length > 0) lines.Add($"简介：{ms["desc"]}");
            if (total > 0) lines.Add($"物件总数：{total:N0}");
            MapText.Text = string.Join("\n", lines);
        }
        catch { MapText.Text = "还没有读取到地图信息。"; }
    }

    async void BtnRefresh_Click(object sender, RoutedEventArgs e) => await RefreshAll();

    async void BtnSave_Click(object sender, RoutedEventArgs e) => await DoSave();

    async Task DoSave()
    {
        if (!_bridgeAlive) { await Ui.Info(this, "还没有连接", "请先按上面的引导连接游戏。"); return; }
        HeroRing.IsActive = true;
        try
        {
            var r = await Fx.SaveMapFlow();
            if (r["ok"]?.GetValue<bool>() == true)
                Ui.Banner(ResultBar, "已保存", "地图已写入游戏存档。建议保存后退出重进核对一次。", true);
            else
                Ui.Banner(ResultBar, "保存失败", "详见任务日志。", false);
            Ui.Log(LogBox, r["ok"]?.GetValue<bool>() == true ? "[完成] 服务端已回执保存" : "[失败] " + r.ToJsonString());
        }
        catch (Exception ex)
        {
            Ui.Banner(ResultBar, "保存失败", ex.Message, false);
        }
        finally { HeroRing.IsActive = false; }
    }

    async void QaBackup_Click(object sender, RoutedEventArgs e)
    {
        if (!_bridgeAlive) { await Ui.Info(this, "还没有连接", "请先按上面的引导连接游戏。"); return; }
        HeroRing.IsActive = true;
        try
        {
            var stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
            var outFile = Path.Combine(Fx.SnapshotsDir, $"backup_{stamp}.json");
            var code = await Job.RunAsync("备份地图", Cmds.SnapshotExport(outFile, null),
                line => Ui.Log(LogBox, line));
            if (code == 0)
                Ui.Banner(ResultBar, "备份成功！",
                    $"备份文件已保存：{outFile}（{Ui.FmtSize(new System.IO.FileInfo(outFile).Length)}）。" +
                    "需要恢复时到「备份与恢复」页选择这份文件。", true);
            else
                Ui.Banner(ResultBar, "备份失败", "详见任务日志。", false);
        }
        catch (Exception ex) { Ui.Banner(ResultBar, "备份失败", ex.Message, false); }
        finally { HeroRing.IsActive = false; }
    }

    async void QaShots_Click(object sender, RoutedEventArgs e)
    {
        if (!_bridgeAlive) { await Ui.Info(this, "还没有连接", "请先按上面的引导连接游戏。"); return; }
        HeroRing.IsActive = true;
        try
        {
            var code = await Job.RunAsync("多角度截图", Cmds.ShotSweep(),
                line => Ui.Log(LogBox, line));
            if (code == 0)
            {
                Ui.Banner(ResultBar, "截图完成！", "5 张截图已保存到 preview\\shots 文件夹，「截图」页可以查看。", true);
                MainWindow.NavigateTo("shots");
            }
            else
                Ui.Banner(ResultBar, "截图失败", "详见任务日志。", false);
        }
        catch (Exception ex) { Ui.Banner(ResultBar, "截图失败", ex.Message, false); }
        finally { HeroRing.IsActive = false; }
    }

    void QaCover_Click(object sender, RoutedEventArgs e) => MainWindow.NavigateTo("cover");

    async void HeroBtn_Click(object sender, RoutedEventArgs e) => await InstallSelected();

    async void BtnInstall_Click(object sender, RoutedEventArgs e) => await InstallSelected();

    async Task InstallSelected()
    {
        var sel = Fx.Sel ?? Bridges.All()[0];
        if (!await Ui.Confirm(this, "安装 / 重连接桥接",
            $"将把桥接装进游戏（弹授权窗口请点「是」）。目标通道：{sel.Name}。继续？"))
            return;
        HeroRing.IsActive = true;
        try
        {
            if (sel.IsMain)
            {
                Bridges.ClearInstallOverride();   // 主桥接绝不带 override，避免装到别的通道
            }
            else
            {
                // 额外桥接：确认记录的 pid 还活着；不在了就重新选
                var pids = Bridges.DwrgPids();
                if (sel.Pid == null || !pids.Any(p => p.pid == sel.Pid))
                {
                    if (pids.Count == 0) { await Ui.Info(this, "没有找到游戏", "没有检测到运行中的 dwrg.exe。"); return; }
                    var used = UsedPids(sel.Name);
                    var pidBox = new ComboBox { MinWidth = 300, Header = "原记录的游戏窗口已不在，选择新的窗口（pid）" };
                    foreach (var (pid, mem) in pids)
                        pidBox.Items.Add($"pid={pid}　内存 {mem}" + (used.Contains(pid) ? "　（已有桥接）" : ""));
                    int firstFree = pids.FindIndex(p => !used.Contains(p.pid));
                    pidBox.SelectedIndex = firstFree >= 0 ? firstFree : 0;
                    var d = new ContentDialog
                    {
                        Title = "选择游戏窗口",
                        Content = pidBox,
                        PrimaryButtonText = "确定",
                        CloseButtonText = "取消",
                        XamlRoot = XamlRoot,
                    };
                    if (await d.ShowAsync() != ContentDialogResult.Primary) return;
                    var chosenPid = pids[Math.Max(0, pidBox.SelectedIndex)].pid;
                    if (used.Contains(chosenPid))
                    {
                        await Ui.Info(this, "这个窗口已经有桥接了",
                            $"pid={chosenPid} 已被别的桥接占用，重复注入会把原连接顶掉。\n请选一个还没有桥接的游戏窗口。");
                        return;
                    }
                    sel.Pid = chosenPid;
                    Bridges.UpdatePid(sel.Name, sel.Pid);
                }
                Bridges.WriteInstallOverride(sel);
            }
            var code = await Job.RunAsync("安装桥接", Cmds.BridgeInstall(),
                line => Ui.Log(LogBox, line));
            Bridges.ClearInstallOverride();
            if (code == 0)
            {
                Ui.Banner(ResultBar, "已重新连接", $"桥接「{sel.Name}」已就绪。", true);
                FillBridgeSelector();
            }
            else
                Ui.Banner(ResultBar, "连接失败", "详见任务日志（常见原因：还没进编辑器、权限不足）。", false);
        }
        catch (Exception ex) { Ui.Banner(ResultBar, "连接失败", ex.Message, false); }
        finally { HeroRing.IsActive = false; }
        await RefreshHero();
    }

    async void BtnDirty_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var v = await Fx.CallOp("save_dirty");
            await Ui.Info(this, "脏缓冲状态（高级）", Ui.Pretty(v));
        }
        catch (Exception ex) { await Ui.Info(this, "查询失败", ex.Message); }
    }

    // ---- 重建导出表（游戏更新后防崩闸门拒绝注入时的修复入口） ----
    bool _exportsBusy;

    async void BtnExports_Click(object sender, RoutedEventArgs e) => await RebuildExports();

    async Task RebuildExports()
    {
        if (_exportsBusy) return;
        if (!await Ui.Confirm(this, "重建导出表",
            "游戏大版本更新后，导出表（neox_exports.txt）会整体过期，装桥会被防崩闸门拒绝。\n" +
            "这一步会读取当前引擎重建导出表（旧表自动备份 .bak），并做与闸门同源的核对。\n继续？"))
            return;

        var dll = Exports.LocateEngineDll();
        if (dll == null)
        {
            if (!await Ui.Confirm(this, "没有自动找到引擎文件",
                "没有检测到运行中的游戏（或模块读取受限）。\n" +
                "手动选择游戏安装目录下的 neox_engine.dll 继续吗？"))
                return;
            var f = await Ui.PickFile(this, ".dll");
            if (f == null) return;
            dll = f.Path;
        }

        _exportsBusy = true;
        BtnExports.IsEnabled = false;
        try
        {
            Ui.Log(LogBox, $"目标引擎：{dll}");
            var code = await Ui.RunCli(this, LogBox, "重建导出表", Exports.BuildArgv(dll));
            if (code != 0)
            {
                await Ui.Info(this, "重建失败",
                    "cli/fxexports.py build 没有跑成功（exit " + code + "），原因见任务日志。");
                return;
            }
            code = await Ui.RunCli(this, LogBox, "核对导出表", Exports.VerifyArgv(dll));
            if (code != 0)
            {
                await Ui.Info(this, "核对未通过",
                    "新表与引擎核对失败（exit " + code + "），原因见任务日志。\n" +
                    "一般说明选到的 DLL 与运行中的游戏不是同一份。");
                return;
            }
            await Ui.Info(this, "导出表已重建",
                "新表已写入 " + Exports.TablePath + " 并与当前引擎核对一致。\n" +
                "现在可以点「安装 / 重注入桥接」装桥了。");
        }
        catch (Exception ex)
        {
            await Ui.Info(this, "重建导出表失败", ex.Message);
        }
        finally
        {
            _exportsBusy = false;
            BtnExports.IsEnabled = true;
        }
    }
}
