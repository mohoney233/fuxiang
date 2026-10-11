using FuxiangConsole.Services;
using Microsoft.UI.Xaml.Controls;

namespace FuxiangConsole.Pages;

public sealed partial class AboutPage : Page
{
    public AboutPage()
    {
        InitializeComponent();
        try
        {
            var v = System.Reflection.Assembly.GetExecutingAssembly().GetName().Version;
            VersionText.Text = v is null ? "" : $"版本 {v.Major}.{v.Minor}.{v.Build}";
        }
        catch { }
    }
}
