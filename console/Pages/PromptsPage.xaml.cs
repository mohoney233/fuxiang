using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace FuxiangConsole.Pages;

public sealed partial class PromptsPage : Page
{
    sealed record PromptDoc(string Title, string Desc, string Text);

    static readonly string[] CatOrder =
    {
        "桥接与工具链", "地图与备份", "摆放与编辑", "剧情", "事件", "队伍", "封面与截图", "综合协作",
    };

    static readonly (string Cat, PromptDoc P)[] Prompts =
    {
        // 桥接与工具链：教 AI 助手认识并正确使用桥
        ("桥接与工具链", new PromptDoc("环境接线（新会话先发这条）", "让 AI 认识桥接工具链，摸清环境再动手",
            "你在「随笔浮想」编辑器桥接工具链下工作。游戏内桥的唯一真源是命令行：python cli/fxapi.py <op> '<json 参数>'（在工具根目录执行，stdout 返回业务 JSON）。请先执行 status 和 save_dirty 摸清当前环境，用中文汇报，不要做任何改动。")),
        ("桥接与工具链", new PromptDoc("检查桥接是否在线", "心跳超过 10 秒即视为离线",
            "先检查桥接是否在线：读 ipc/bridge_state.json，其中 now 字段距当前时间超过 10 秒即离线；再跑一次 status 确认编辑器核心可用。把两项结论告诉我，然后等我指令。")),
        ("桥接与工具链", new PromptDoc("安全操作规约", "让 AI 全程遵守的工作方式",
            "操作本编辑器请全程遵守：① 会改动地图的操作先备份；② 写操作一律先 dry-run / 预览，我确认后再执行；③ 异步命令（place_item、save_map、story_create、cover_upload 等）要轮询对应的 *_poll 直到拿到回执；④ 删除前先用 delete_preview 看级联影响；⑤ 全部完成后提醒我保存地图。")),
        ("桥接与工具链", new PromptDoc("桥掉线了怎么排查", "按顺序自检并汇报",
            "桥接好像掉线了，请按顺序排查并汇报：① dwrg.exe 是否在运行；② ipc/bridge_state.json 的心跳是否过期；③ 若游戏刚重启，提醒我用管理员权限重装桥（控制台首页「安装 / 重注入桥接」按钮，或管理员运行 fuxiang-inject.exe install）；④ 恢复后用 status 验证并告诉我结果。")),
        ("桥接与工具链", new PromptDoc("多开窗口对应哪条通道", "每个游戏窗口用各自的 cli 副本",
            "多开游戏时每条桥接通道有独立的 cli 副本：console/bridges/<通道名>/cli/fxapi.py，跑哪个副本就操作哪个游戏窗口。请先列出 console/bridges/ 下现有的通道，读各通道 ipc/bridge_state.json 告诉我哪条在线，不要调用任何写操作。")),

        // 地图与备份
        ("地图与备份", new PromptDoc("了解当前地图", "摸清地图概况，不做任何改动",
            "请读一下当前地图的信息（名称、简介、玩法设置、组件类型分布），用中文概括这张地图的玩法和结构，不要改动任何内容。")),
        ("地图与备份", new PromptDoc("备份当前地图", "完整存档一份，不动地图",
            "帮我完整备份一次当前地图，完成后告诉我备份文件名和保存路径。")),
        ("地图与备份", new PromptDoc("恢复到某个备份", "先预览会改动什么，确认后再执行",
            "列出最近的地图备份，我想恢复到其中一个；恢复前先给我看预览（会改动什么），我确认后再执行。")),
        ("地图与备份", new PromptDoc("保存地图", "把改动写入游戏存档",
            "请把当前所有改动保存进游戏存档，保存完成后确认回执状态并告诉我。")),

        // 摆放与编辑
        ("摆放与编辑", new PromptDoc("搜索场景物件", "找到东西的编号和坐标",
            "帮我搜一下场景里名字带「桌子」的物件，列出它们的名称、编号和坐标。")),
        ("摆放与编辑", new PromptDoc("批量摆放物件", "先出摆放计划预览，再执行",
            "在坐标 (0,0,0) 附近每隔 5 米摆放一棵树，共 10 棵排成一排；先给我摆放计划预览，确认可行后再执行。")),
        ("摆放与编辑", new PromptDoc("调整物件位置", "平移 / 旋转 / 缩放",
            "把场景里所有「路灯」沿 X 轴平移 2 米，改之前先列清单给我确认。")),
        ("摆放与编辑", new PromptDoc("删除物件", "先看会删到什么（含组合级联）",
            "预览删除场景中所有「路障」的影响（包括组合级联），我确认后再真正删除。")),

        // 剧情
        ("剧情", new PromptDoc("导出剧情", "存成文件并概括结构",
            "把当前地图的剧情树导出成 Markdown 文件保存到桌面，然后用中文概括剧情结构。")),
        ("剧情", new PromptDoc("导入剧情", "先预览计划，确认后再写入",
            "读取 D:/story.json 的剧情文件，先做导入预览（不写入），告诉我会新建和修改哪些节点，我确认后再应用。")),
        ("剧情", new PromptDoc("检查剧情断点", "找没有下文的节点",
            "遍历剧情树，找出没有台词或没有后续分支的断头节点，列出来给我。")),

        // 事件
        ("事件", new PromptDoc("梳理事件绑定", "触发与接收关系一览",
            "列出场景所有事件键和绑定关系，按「触发 → 接收」的格式整理成表格给我。")),
        ("事件", new PromptDoc("新建事件", "建事件键并绑定到物件",
            "新建一个叫「开门机关」的事件键，绑定到场景里的门组件上，绑定信息的写法参考相邻的既有事件。")),

        // 队伍
        ("队伍", new PromptDoc("读队伍配置", "用人话汇报当前配置",
            "读取当前队伍配置，告诉我有几名求生者、几名监管者，以及各自的出生点位置。")),
        ("队伍", new PromptDoc("添加队伍槽位", "创建完成后提醒保存",
            "再添加一个求生者槽位，位置放在地图中心附近，创建完成后提醒我保存地图。")),

        // 封面与截图
        ("封面与截图", new PromptDoc("更换地图封面", "选图、裁剪并上传",
            "把 D:/Pictures/cover.jpg 设置为地图封面（自动裁成 16:9 并压缩），上传后回读确认给我看。")),
        ("封面与截图", new PromptDoc("拍多角度截图", "俯视 + 四个斜角共 5 张",
            "给当前地图拍一组多角度截图（俯视加四个斜角，共 5 张），完成后告诉我保存在哪里。")),
        ("封面与截图", new PromptDoc("拍俯视平面图", "完整覆盖全部组件范围",
            "拍一张整张地图的俯视平面图，要求完整覆盖所有组件的范围。")),

        // 综合协作
        ("综合协作", new PromptDoc("地图全面体检", "汇总问题清单",
            "对当前地图做一次全面检查：有没有未保存的改动、孤立的组件、断头的剧情、未绑定的事件，把发现汇总成清单。")),
        ("综合协作", new PromptDoc("规划一张新地图", "先出布局方案，确认后施工",
            "我想做一张雪地村庄主题的对战地图。先给我布局方案（区域划分和关键物件清单），我确认后再逐步开始摆放。")),
    };

    public PromptsPage()
    {
        InitializeComponent();
        Render();
    }

    void Render()
    {
        PromptList.Children.Clear();
        foreach (var cat in CatOrder)
        {
            var docs = Prompts.Where(x => x.Cat == cat).Select(x => x.P).ToArray();
            if (docs.Length == 0) continue;

            var panel = new StackPanel { Spacing = 8 };
            foreach (var p in docs) panel.Children.Add(PromptCard(p));

            PromptList.Children.Add(new Expander
            {
                Header = new TextBlock
                {
                    Text = $"{cat}（{docs.Length}）",
                    FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                },
                Content = panel,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
            });
        }
    }

    FrameworkElement PromptCard(PromptDoc p)
    {
        var card = new Border
        {
            Padding = new Thickness(12),
            CornerRadius = new CornerRadius(4),
            Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.Gray) { Opacity = 0.10 },
        };
        var text = new TextBlock
        {
            Text = p.Text,
            FontSize = 13,
            LineHeight = 20,
            IsTextSelectionEnabled = true,
            TextWrapping = TextWrapping.Wrap,
        };
        card.Child = text;

        var copy = new Button { Content = "复制", Padding = new Thickness(12, 4, 12, 4), VerticalAlignment = VerticalAlignment.Top };
        copy.Click += async (_, _) => await CopyPrompt(copy, p.Text);

        var head = new Grid { ColumnSpacing = 12 };
        head.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        head.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var titles = new StackPanel { Spacing = 2 };
        titles.Children.Add(new TextBlock { Text = p.Title, FontWeight = Microsoft.UI.Text.FontWeights.SemiBold });
        titles.Children.Add(new TextBlock { Text = p.Desc, FontSize = 12, Opacity = 0.7 });
        Grid.SetColumn(titles, 0);
        Grid.SetColumn(copy, 1);
        head.Children.Add(titles);
        head.Children.Add(copy);

        var root = new StackPanel { Spacing = 8 };
        root.Children.Add(head);
        root.Children.Add(card);
        return root;
    }

    static async Task CopyPrompt(Button btn, string text)
    {
        bool ok = false;
        try
        {
            var pkg = new Windows.ApplicationModel.DataTransfer.DataPackage();
            pkg.SetText(text);
            Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(pkg);
            ok = true;
        }
        catch { /* 剪贴板被其他进程占用时可能抛异常，忽略即可 */ }

        btn.Content = ok ? "已复制" : "复制失败";
        btn.IsEnabled = false;
        await Task.Delay(1200);
        btn.Content = "复制";
        btn.IsEnabled = true;
    }
}
