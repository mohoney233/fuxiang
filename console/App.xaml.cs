using Microsoft.UI.Xaml;

namespace FuxiangConsole;

public partial class App : Application
{
    public static MainWindow Main;

    public App()
    {
        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        Main = new MainWindow();
        Main.Activate();
    }
}
