using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;

namespace FuxiangConsole.Services;

/// <summary>
/// 与游戏内桥通信的唯一真源是 cli/fxapi.py（ipc/cmd + ipc/out 文件 IPC）。
/// 本层通过 python CLI 调用它，避免在 C# 里复刻剥壳协议。
/// </summary>
public static class Fx
{
    public static string Root { get; } = FindRoot();
    public static string CliDir => Path.Combine(Root, "cli");
    public static string IpcDir => Path.Combine(Root, "ipc");
    public static string SnapshotsDir => Path.Combine(Root, "snapshots");
    public static string StoryDir => Path.Combine(Root, "projects", "story");
    public static string EventDir => Path.Combine(Root, "projects", "event_dump");
    public static string ShotsDir => Path.Combine(Root, "preview", "shots");
    public static string RestoreDir => Path.Combine(Root, "builds", "restore");
    public static string BridgesDir => Path.Combine(Root, "console", "bridges");

    static string FindRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir != null)
        {
            if (File.Exists(Path.Combine(dir.FullName, "cli", "fxapi.py")))
                return dir.FullName;
            dir = dir.Parent;
        }
        return AppContext.BaseDirectory;
    }

    // ---- python 解释器定位（可在 settings.json 覆盖；PythonExe 依次探测 PATH 上的 python/python3/py） ----
    static string[] CandidatePythons =
    {
        "python",
        "python3",
        "py",
    };

    public static string PythonExe
    {
        get
        {
            var cfg = Settings.Load();
            if (!string.IsNullOrWhiteSpace(cfg.Python)) return cfg.Python;
            foreach (var c in CandidatePythons)
                if (c.Contains('\\') && c.Contains(':') && File.Exists(c)) return c;
            return "python";
        }
    }

    // ---- 桥接实例（多开游戏 = 多条独立通道；每条通道 = 一个目录：bridge/ cli/ ipc/） ----
    public sealed class BridgeInstance
    {
        public string Name { get; set; }
        public string Dir { get; set; }
        public long? Pid { get; set; }
        public bool IsMain => Dir == Root;
        public string CliDir => Path.Combine(Dir, "cli");
        public string IpcDir => Path.Combine(Dir, "ipc");
        public string StatePath => Path.Combine(IpcDir, "bridge_state.json");
        public override string ToString() => Name;
    }

    static BridgeInstance _sel;

    /// <summary>当前选中的桥接实例；所有 op / 任务 / 状态读取都走它的通道。</summary>
    public static BridgeInstance Sel => _sel ??= Bridges.LoadSelected();

    public static void SetSelected(BridgeInstance inst)
    {
        _sel = inst;
        var s = Settings.Load();
        s.SelectedBridge = inst?.Name;
        s.Save();
    }

    // ---- 桥接状态（直读当前实例的心跳文件，不触桥） ----
    static long? Num(JsonNode n)
    {
        if (n is JsonValue v && v.TryGetValue<double>(out var d)) return (long)d;
        return null;
    }

    static double? NumD(JsonNode n)
    {
        if (n is JsonValue v && v.TryGetValue<double>(out var d)) return d;
        return null;
    }

    public sealed class BridgeState
    {
        public bool Alive; public double? Age; public long? Pid; public string Version;
        public double? StartedAt; public long? Polls; public string BusyOp;
        public string Raw;
    }

    public static BridgeState ReadBridgeState()
    {
        var st = new BridgeState();
        try
        {
            var path = Sel.StatePath;
            if (!File.Exists(path)) return st;
            st.Raw = File.ReadAllText(path);
            var n = JsonNode.Parse(st.Raw) as JsonObject;
            if (n == null) return st;
            st.Version = n["version"]?.ToString();
            st.Pid = Num(n["pid"]);
            st.StartedAt = NumD(n["started_at"]);
            st.Polls = Num(n["polls"]);
            st.BusyOp = n["busy_op"]?.ToString();
            if (NumD(n["now"]) is double now)
            {
                st.Age = Math.Round(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - now, 1);
                st.Alive = st.Age <= 10.0;
            }
        }
        catch { /* 心跳文件亚毫秒替换窗口可能瞬时读不到 */ }
        return st;
    }

    static DateTime _dwrgAt = DateTime.MinValue;
    static bool _dwrgOk;
    static readonly SemaphoreSlim _dwrgGate = new(1, 1);

    static bool RunTasklistCheck()
    {
        try
        {
            using var p = Process.Start(new ProcessStartInfo("tasklist",
                "/FI \"IMAGENAME eq dwrg.exe\" /FO CSV /NH")
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                CreateNoWindow = true,
            });
            var outp = p.StandardOutput.ReadToEnd();
            p.WaitForExit(8000);
            return outp != null && outp.Contains("dwrg.exe", StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    /// <summary>同步版（带 5s 缓存）。可能阻塞当前线程最长 ~8s，不要在 UI 线程直接调。</summary>
    public static bool DwrgRunning()
    {
        if ((DateTime.UtcNow - _dwrgAt).TotalSeconds < 5) return _dwrgOk;
        _dwrgOk = RunTasklistCheck();
        _dwrgAt = DateTime.UtcNow;
        return _dwrgOk;
    }

    /// <summary>异步版：tasklist 在线程池跑，UI 线程安全。</summary>
    public static async Task<bool> DwrgRunningAsync()
    {
        if ((DateTime.UtcNow - _dwrgAt).TotalSeconds < 5) return _dwrgOk;
        await _dwrgGate.WaitAsync();
        try
        {
            if ((DateTime.UtcNow - _dwrgAt).TotalSeconds < 5) return _dwrgOk;
            _dwrgOk = await Task.Run(RunTasklistCheck);
            _dwrgAt = DateTime.UtcNow;
            return _dwrgOk;
        }
        finally { _dwrgGate.Release(); }
    }

    // ---- op / eval 调用（每次起一个 python 进程；走当前选中桥接实例的通道） ----
    public static async Task<JsonNode> CallOp(string op, JsonNode args = null, int timeoutSec = 120)
    {
        var json = args?.ToJsonString() ?? "{}";
        var psi = new ProcessStartInfo(PythonExe)
        {
            WorkingDirectory = Root,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        psi.ArgumentList.Add("-u");
        psi.ArgumentList.Add(Path.Combine(Sel.CliDir, "fxapi.py"));
        psi.ArgumentList.Add(op);
        psi.ArgumentList.Add(json);
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        psi.Environment["PYTHONUNBUFFERED"] = "1";

        using var p = Process.Start(psi);
        // 两根管道并行读：串行 await 会在子进程写满 stderr 缓冲时死锁
        var outTask = p.StandardOutput.ReadToEndAsync();
        var errTask = p.StandardError.ReadToEndAsync();
        await Task.WhenAll(outTask, errTask);
        var stdout = outTask.Result;
        var stderr = errTask.Result;
        if (!p.WaitForExit(timeoutSec * 1000))
        {
            try { p.Kill(true); } catch { }
            throw new InvalidOperationException($"{op} 超时（{timeoutSec}s）");
        }
        if (p.ExitCode != 0)
            throw new InvalidOperationException($"{op} 失败（exit {p.ExitCode}）：{(string.IsNullOrWhiteSpace(stderr) ? stdout : stderr).Trim()}");
        try { return JsonNode.Parse(stdout); }
        catch (Exception e) { throw new InvalidOperationException($"{op} 返回不是合法 JSON：{e.Message}\n{stdout}"); }
    }

    // eval：L3 业务值是 {"result": X}，再剥一层；jsonResult 时 X 若为字符串按 JSON 解析。
    public static async Task<JsonNode> Eval(string code, bool jsonResult = false, int timeoutSec = 120)
    {
        var v = await CallOp("eval", new JsonObject { ["code"] = code }, timeoutSec);
        if (v is JsonObject o && o.Count == 1 && o.ContainsKey("result"))
            v = o["result"];
        if (jsonResult && v is JsonValue jv && jv.TryGetValue<string>(out var s))
        {
            try { return JsonNode.Parse(s); }
            catch (Exception e) { throw new InvalidOperationException("__result__ 不是合法 JSON：" + e.Message + "\n" + s); }
        }
        return v;
    }

    public static string Cli(string name) => Path.Combine(Sel.CliDir, name);

    public static string Quote(string s)
    {
        if (s.Length > 1 && s.StartsWith('"')) return s;
        return "\"" + s.Replace("\"", "\\\"") + "\"";
    }

    // ---- 真实保存（save_map + save_map_poll 编排） ----
    public static async Task<JsonObject> SaveMapFlow(int timeoutSec = 180)
    {
        var first = await CallOp("save_map") as JsonObject;
        var rid = first?["request_id"]?.ToString();
        if (string.IsNullOrEmpty(rid))
            throw new InvalidOperationException("save_map 未返回 request_id：" + (first?.ToJsonString() ?? "null"));
        var deadline = DateTime.UtcNow.AddSeconds(timeoutSec);
        JsonNode last = null;
        while (DateTime.UtcNow < deadline)
        {
            last = await CallOp("save_map_poll", new JsonObject { ["request_id"] = rid });
            if (last?["status"]?.ToString() == "saved")
                return new JsonObject { ["ok"] = true, ["poll"] = last };
            await Task.Delay(1000);
        }
        return new JsonObject { ["ok"] = false, ["error"] = $"保存轮询超时（{timeoutSec}s）", ["last"] = last };
    }

    // ---- 阵营槽位创建（§8.3 口径：摆放通道 + 创建即带 pos） ----
    public static async Task<JsonObject> TeamAddSlot(int itemId, double[] pos, string label)
    {
        var rid = $"camp_{label}_{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}";
        var placeArgs = new JsonObject
        {
            ["item_id"] = itemId,
            ["request_id"] = rid,
            ["position"] = new JsonArray(pos[0], pos[1], pos[2]),
            ["mode"] = "server",
            ["allow_unverified"] = true,
        };
        var place = await CallOp("place_item", placeArgs);
        JsonNode poll = null;
        for (int i = 0; i < 12; i++)
        {
            poll = await CallOp("place_poll", new JsonObject { ["request_id"] = rid });
            if (poll?["status"]?.ToString() == "placed") break;
            if (poll?["status"]?.ToString() == "pending") { await Task.Delay(150); continue; }
            break;
        }
        var ok = poll?["status"]?.ToString() == "placed";
        return new JsonObject
        {
            ["role"] = label, ["item_id"] = itemId,
            ["ok"] = ok, ["place"] = place, ["poll"] = poll,
        };
    }
}

/// <summary>console/settings.json（python 路径等）。</summary>
public class Settings
{
    public string Python { get; set; }
    public List<string> ConsoleHistory { get; set; } = new();
    public bool FirstRunDone { get; set; }
    public string SelectedBridge { get; set; }
    public bool SkipRiskDialog { get; set; }

    static string Path_ => Path.Combine(Fx.Root, "console", "settings.json");

    public static Settings Load()
    {
        try
        {
            var s = File.ReadAllText(Path_);
            return System.Text.Json.JsonSerializer.Deserialize<Settings>(s) ?? new Settings();
        }
        catch { return new Settings(); }
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path_));
            File.WriteAllText(Path_, System.Text.Json.JsonSerializer.Serialize(this,
                new System.Text.Json.JsonSerializerOptions { WriteIndented = true, Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping }));
        }
        catch { }
    }
}
