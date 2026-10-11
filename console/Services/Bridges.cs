using System.Text.Json;
using System.Text.Json.Nodes;

namespace FuxiangConsole.Services;

/// <summary>
/// 多桥接实例管理。每条通道 = 一个独立目录（bridge/ + cli/ + ipc/）：
/// 游戏内的桥按自己脚本位置推导通道，所以给第二个游戏窗口装桥时，
/// 用注入器的 install_override.json 把目标 pid 和一份通道克隆指给它。
/// 注册表存 console/bridges/index.json。
/// </summary>
public static class Bridges
{
    static string IndexPath => Path.Combine(Fx.BridgesDir, "index.json");

    public static List<Fx.BridgeInstance> All()
    {
        var list = new List<Fx.BridgeInstance>
        {
            new Fx.BridgeInstance { Name = "主桥接", Dir = Fx.Root },
        };
        try
        {
            if (File.Exists(IndexPath))
            {
                var arr = JsonNode.Parse(File.ReadAllText(IndexPath)) as JsonArray;
                foreach (var n in arr ?? new JsonArray())
                {
                    var name = n?["name"]?.ToString();
                    var dir = n?["dir"]?.ToString();
                    if (string.IsNullOrEmpty(name) || string.IsNullOrEmpty(dir)) continue;
                    if (!Directory.Exists(dir)) continue;
                    long? pid = null;
                    if (n?["pid"] is JsonValue pv && pv.TryGetValue<double>(out var d)) pid = (long)d;
                    list.Add(new Fx.BridgeInstance { Name = name, Dir = dir, Pid = pid });
                }
            }
        }
        catch { /* 注册表坏了就当只有主桥接 */ }
        return list;
    }

    static void SaveAll(IEnumerable<Fx.BridgeInstance> slots)
    {
        Directory.CreateDirectory(Fx.BridgesDir);
        var arr = new JsonArray();
        foreach (var s in slots)
            arr.Add(new JsonObject { ["name"] = s.Name, ["dir"] = s.Dir, ["pid"] = s.Pid });
        File.WriteAllText(IndexPath, arr.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
    }

    public static Fx.BridgeInstance LoadSelected()
    {
        var all = All();
        var want = Settings.Load().SelectedBridge;
        foreach (var i in all)
            if (i.Name == want) return i;
        return all[0];
    }

    /// <summary>列举所有 dwrg.exe 进程 pid（供「添加桥接」选择目标窗口）。</summary>
    public static List<(long pid, string mem)> DwrgPids()
    {
        var r = new List<(long, string)>();
        try
        {
            using var p = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(
                "tasklist", "/FI \"IMAGENAME eq dwrg.exe\" /FO CSV /NH")
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                CreateNoWindow = true,
            });
            var outp = p.StandardOutput.ReadToEnd();
            p.WaitForExit(10000);
            foreach (var line in (outp ?? "").Split('\n'))
            {
                // 内存列自带千分位逗号（"1,321,404 K"），只能按字段间的 "," 切
                var parts = line.TrimEnd('\r', '\n').Split(new[] { "\",\"" }, StringSplitOptions.None);
                if (parts.Length < 5) continue;
                var name = parts[0].Trim('"');
                if (!name.Equals("dwrg.exe", StringComparison.OrdinalIgnoreCase)) continue;
                if (long.TryParse(parts[1].Trim('"'), out var pid))
                    r.Add((pid, parts[4].Trim('"', ' ')));
            }
        }
        catch { }
        return r;
    }

    /// <summary>为新桥接建一条独立通道：克隆 bridge/、cli/ 与 mcp/（各几十个 py，秒级）。</summary>
    public static Fx.BridgeInstance CreateSlot(string name, long? pid)
    {
        var dir = Path.Combine(Fx.BridgesDir, SafeName(name));
        Directory.CreateDirectory(dir);
        CopyDir(Path.Combine(Fx.Root, "bridge"), Path.Combine(dir, "bridge"));
        CopyDir(Path.Combine(Fx.Root, "cli"), Path.Combine(dir, "cli"));
        // cli/fxshot.py 按自身根加载 mcp/preview.py，缺了它截图页在这条通道上直接起不来
        CopyDir(Path.Combine(Fx.Root, "mcp"), Path.Combine(dir, "mcp"));
        Directory.CreateDirectory(Path.Combine(dir, "ipc", "cmd"));
        Directory.CreateDirectory(Path.Combine(dir, "ipc", "out"));
        var inst = new Fx.BridgeInstance { Name = name, Dir = dir, Pid = pid };
        var slots = All().Where(x => !x.IsMain).ToList();
        slots.Add(inst);
        SaveAll(slots);
        return inst;
    }

    public static void UpdatePid(string name, long? pid)
    {
        var slots = All().Where(x => !x.IsMain).ToList();
        foreach (var s in slots)
            if (s.Name == name) s.Pid = pid;
        SaveAll(slots);
    }

    /// <summary>从注册表移除（不删目录，桥可能仍在游戏里跑着）。</summary>
    public static void Remove(string name)
    {
        var slots = All().Where(x => !x.IsMain && x.Name != name).ToList();
        SaveAll(slots);
    }

    static string SafeName(string n)
    {
        var s = new string(n.Select(c => char.IsLetterOrDigit(c) || c == '-' || c == '_' ? c : '_').ToArray());
        return string.IsNullOrWhiteSpace(s) ? "slot" : s;
    }

    static void CopyDir(string src, string dst)
    {
        Directory.CreateDirectory(dst);
        foreach (var f in Directory.GetFiles(src))
        {
            if (f.EndsWith(".pyc", StringComparison.OrdinalIgnoreCase)) continue;
            File.Copy(f, Path.Combine(dst, Path.GetFileName(f)), true);
        }
    }

    // ---- 安装 override（多实例注入的关键） ----
    static string OverrideFile => Path.Combine(Fx.IpcDir, "install_override.json");

    /// <summary>写 override：把下一次 install 指到指定 pid + 通道。</summary>
    public static void WriteInstallOverride(Fx.BridgeInstance inst)
    {
        Directory.CreateDirectory(Fx.IpcDir);
        var o = new JsonObject
        {
            ["pid"] = inst.Pid,
            ["bridge"] = Path.Combine(inst.Dir, "bridge", "fuxiang_bridge.py"),
            ["ipc"] = inst.IpcDir,
        };
        File.WriteAllText(OverrideFile, o.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
    }

    public static void ClearInstallOverride()
    {
        try { if (File.Exists(OverrideFile)) File.Delete(OverrideFile); } catch { }
    }
}
