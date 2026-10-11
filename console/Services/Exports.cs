using System.Diagnostics;

namespace FuxiangConsole.Services;

/// <summary>
/// 引擎导出表（注入器防崩闸门的真源）的重建与核对。
/// 游戏更新引擎后表会整体过期，装桥被闸门拒绝（exit 9）；这里一键重建：
/// python cli/fxexports.py build 读 DLL 重建（旧表自动备份 .bak），
/// 再用注入器 verify-exports 做与闸门同源的最终核对。
/// 全部走工具根的 cli（fxexports.py 是离线工具，与桥接通道无关）。
/// </summary>
public static class Exports
{
    public static string TablePath =>
        Path.Combine(Fx.Root, "research", "tools_extract", "neox_exports.txt");

    public static string InjectorExe => Path.Combine(
        Fx.Root, "injector", "bin", "Debug", "net8.0-windows", "fuxiang-inject.exe");

    /// <summary>从运行中的 dwrg 进程枚举 neox_engine.dll 的实际路径（只读模块信息，无需提权）。
    /// 游戏没开或模块枚举受限时返回 null，由调用方引导手动选文件。</summary>
    public static string LocateEngineDll()
    {
        try
        {
            foreach (var p in Process.GetProcessesByName("dwrg"))
            {
                try
                {
                    foreach (ProcessModule m in p.Modules)
                        if (string.Equals(m.ModuleName, "neox_engine.dll", StringComparison.OrdinalIgnoreCase))
                            return m.FileName;
                }
                catch { /* 单个进程枚举失败就跳过 */ }
                finally { try { p.Dispose(); } catch { } }
            }
        }
        catch { }
        return null;
    }

    /// <summary>python &lt;工具根&gt;/cli/fxexports.py build &lt;dll&gt;。</summary>
    public static List<string> BuildArgv(string dll) => new()
    {
        Fx.PythonExe, "-u", Path.Combine(Fx.Root, "cli", "fxexports.py"), "build", dll,
    };

    /// <summary>fuxiang-inject verify-exports &lt;dll&gt; &lt;表&gt;（exit 9 = 闸门口径的失败）。</summary>
    public static List<string> VerifyArgv(string dll) => new()
    {
        InjectorExe, "verify-exports", dll, TablePath,
    };
}
