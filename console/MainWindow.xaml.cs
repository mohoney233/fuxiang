using FuxiangConsole.Pages;
using FuxiangConsole.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace FuxiangConsole;

public sealed partial class MainWindow : Window
{
    DispatcherTimer _timer;

    public MainWindow()
    {
        InitializeComponent();
        Title = "随笔浮想 · 桥接控制台";
        SetWindowIcon();
        ContentFrame.Navigate(typeof(DashboardPage));
        Nav.SelectedItem = Nav.MenuItems[0];
        Nav.Loaded += (_, _) => _ = ShowRiskNoticeOnce();

        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
        _timer.Tick += async (_, _) => await RefreshBridgePill();
        _timer.Start();
        _ = RefreshBridgePill();
    }

    bool _riskShown;

    /// <summary>启动时的风险须知弹窗；勾选「下次不再弹出」并确定后写入 settings.json，之后不再弹。</summary>
    async Task ShowRiskNoticeOnce()
    {
        if (_riskShown) return;
        _riskShown = true;
        try
        {
            if (Settings.Load().SkipRiskDialog) return;
            var agree = new CheckBox { Content = "下次不再弹出" };
            var sp = new StackPanel { Spacing = 14 };
            sp.Children.Add(new TextBlock
            {
                Text = "使用该软件可能会使账号面临违规风险，继续使用则代表你已知晓并承担风险，造成的损失作者概不负责。",
                TextWrapping = TextWrapping.Wrap,
            });
            sp.Children.Add(agree);
            var d = new ContentDialog
            {
                Title = "使用须知",
                Content = sp,
                PrimaryButtonText = "确定",
                DefaultButton = ContentDialogButton.Primary,
                XamlRoot = Nav.XamlRoot,
            };
            await d.ShowAsync();
            if (agree.IsChecked == true)
            {
                var s = Settings.Load();
                s.SkipRiskDialog = true;
                s.Save();
            }
        }
        catch { /* 弹窗失败不应影响启动 */ }
    }

    /// <summary>标题栏与任务栏图标（unpackaged 应用要手动指定 ico 文件）。</summary>
    void SetWindowIcon()
    {
        try
        {
            AppWindow.SetIcon(Path.Combine(AppContext.BaseDirectory, "app.ico"));
        }
        catch { }
    }

    void Nav_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.SelectedItem is not NavigationViewItem item || item.Tag is null) return;
        var page = item.Tag switch
        {
            "dashboard" => typeof(DashboardPage),
            "console" => typeof(ConsolePage),
            "snapshots" => typeof(SnapshotPage),
            "story" => typeof(StoryPage),
            "events" => typeof(EventPage),
            "teams" => typeof(TeamPage),
            "cover" => typeof(CoverPage),
            "shots" => typeof(ShotPage),
            "prompts" => typeof(PromptsPage),
            "about" => typeof(AboutPage),
            _ => null,
        };
        if (page != null && ContentFrame.CurrentSourcePageType != page)
            ContentFrame.Navigate(page);
    }

    /// <summary>供页面跳转到指定导航项（如首页快捷按钮跳到封面页）。</summary>
    public static void NavigateTo(string tag)
    {
        var win = App.Main;
        foreach (var item in win.Nav.MenuItems.Concat(win.Nav.FooterMenuItems).OfType<NavigationViewItem>())
        {
            if ((string)item.Tag == tag)
            {
                win.Nav.SelectedItem = item;
                return;
            }
        }
    }

    async Task RefreshBridgePill()
    {
        var st = Fx.ReadBridgeState();
        var dwrg = await Fx.DwrgRunningAsync();
        Brush ok = new SolidColorBrush(Color.FromArgb(255, 63, 185, 80));
        Brush bad = new SolidColorBrush(Color.FromArgb(255, 248, 81, 73));
        Brush warn = new SolidColorBrush(Color.FromArgb(255, 210, 153, 34));
        if (st.Alive)
        {
            BridgeDot.Fill = ok;
            // 只读了心跳文件，不知道地图编辑器开没开，所以别说成「已连接编辑器」——首页 hero 才给准确引导
            BridgeText.Text = "桥接已连上";
        }
        else
        {
            BridgeDot.Fill = dwrg ? warn : bad;
            // 左下角宽度有限（190px 一行），细节引导交给首页 hero，这里只留最短结论
            BridgeText.Text = dwrg ? "需要重新连接，看首页" : "游戏还没打开";
        }
        await Task.CompletedTask;
    }
}
