using System.Diagnostics;
using System.Text.Json.Nodes;

namespace FuxiangConsole.Services;

/// <summary>长任务（CLI 子进程）的 argv 构造。全部对应 cli/ 下已实测脚本。</summary>
public static class Cmds
{
    static List<string> Py(string script, params object[] args)
    {
        var argv = new List<string> { Fx.PythonExe, "-u", Fx.Cli(script) };
        foreach (var a in args)
        {
            if (a == null) continue;
            var s = a.ToString();
            if (s.Length == 0) continue;
            argv.Add(s);
        }
        return argv;
    }

    static List<string> Py(object[] args) => Py((string)args[0], args.Skip(1).ToArray());

    // -ToolRoot：控制台用 -File 拉起时 $PSScriptRoot 是空的（实机踩到），显式交工具根
    public static List<string> BridgeInstall() => new()
    {
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        Path.Combine(Fx.Root, "fuxiang_bridge_install.ps1"), "-ToolRoot", Fx.Root,
    };

    // ---- 快照 ----
    public static List<string> SnapshotExport(string outFile, int? terrainStep) =>
        Py("snapshot.py", "export",
           outFile != null ? "--out" : null, outFile,
           terrainStep != null ? "--terrain" : null, terrainStep);
    public static List<string> SnapshotInspect(string file) => Py("snapshot.py", "inspect", file);
    public static List<string> SnapshotCheck(string file) => Py("snapshot.py", "check", file);
    public static List<string> SnapshotDiff(string a, string b, double? tol, bool identity) =>
        Py("snapshot.py", "diff", a, b,
           tol != null ? "--tol" : null, tol?.ToString(),
           identity ? "--include-identity" : null);

    public static List<string> SnapshotRestore(RestoreOpt o)
    {
        var args = new List<object> { "snapshot_restore.py", o.File };
        if (o.Apply) { args.Add("--apply"); args.Add("--yes"); }
        void Flag(string name, bool on) { if (on) args.Add(name); }
        Flag("--with-story", o.WithStory); Flag("--include-ui", o.IncludeUi);
        Flag("--include-motion", o.IncludeMotion); Flag("--include-global", o.IncludeGlobal);
        Flag("--include-not-placeable", o.IncludeNotPlaceable);
        Flag("--no-props", o.NoProps); Flag("--no-events", o.NoEvents);
        Flag("--sync-settings", o.SyncSettings); Flag("--set-framework", o.SetFramework);
        Flag("--sync-global", o.SyncGlobal); Flag("--set-bgm", o.SetBgm);
        Flag("--new-run", o.NewRun);
        Flag("--allow-sentinel", o.AllowSentinel); Flag("--allow-same-map", o.AllowSameMap);
        Flag("--allow-dangling-refs", o.AllowDanglingRefs);
        Flag("--allow-missing-stages", o.AllowMissingStages);
        Flag("--allow-stale-progress", o.AllowStaleProgress);
        Flag("--force-story-import", o.ForceStoryImport);
        void Opt(string name, string v) { if (!string.IsNullOrWhiteSpace(v)) { args.Add(name); args.Add(v); } }
        Opt("--limit", o.Limit); Opt("--batch", o.Batch); Opt("--placement-batch", o.PlacementBatch);
        Opt("--props-batch", o.PropsBatch); Opt("--abort-after", o.AbortAfter);
        Opt("--resume-from", o.ResumeFrom);
        // 进度/idmap 文件固定落主工具根的 builds/restore：CLI 默认按自身根解析，
        // 额外桥接用的是通道克隆（console/bridges/<名>/），不补就会把进度写进通道目录。
        Opt("--progress", string.IsNullOrWhiteSpace(o.Progress) ? DefaultProgress(o.File) : o.Progress);
        Opt("--only-types", o.OnlyTypes); Opt("--only-ids", o.OnlyIds);
        return Py(args.ToArray());
    }

    /// <summary>文件名与 cli/snapshot_restore.py 的默认一致，但定到主工具根（idmap 跟着 progress 走）。</summary>
    static string DefaultProgress(string snapshotFile) =>
        string.IsNullOrEmpty(snapshotFile) ? null
        : Path.Combine(Fx.RestoreDir, Path.GetFileNameWithoutExtension(snapshotFile) + ".progress.jsonl");

    public sealed class RestoreOpt
    {
        public string File;
        public bool Apply, WithStory, IncludeUi, IncludeMotion, IncludeGlobal, IncludeNotPlaceable;
        public bool NoProps, NoEvents, SyncSettings, SetFramework, SyncGlobal, SetBgm, NewRun;
        public bool AllowSentinel, AllowSameMap, AllowDanglingRefs, AllowMissingStages, AllowStaleProgress, ForceStoryImport;
        public string Limit, Batch, PlacementBatch, PropsBatch, AbortAfter, ResumeFrom, Progress, OnlyTypes, OnlyIds;
    }

    // ---- 剧情 ----
    public static List<string> StoryExport(string fmt, string outFile) =>
        Py("story.py", "export", "--format", fmt, "--out", outFile);
    public static List<string> StoryImport(string file, bool apply, bool prune) =>
        Py("story.py", "import", file, apply ? "--apply" : null, prune ? "--prune" : null);
    public static List<string> StoryResume() => Py("story.py", "resume");
    public static List<string> StorySet(string id, string[] fields, bool dry)
    {
        var a = new List<object> { "story.py", "set", id };
        foreach (var f in fields) { a.Add("-f"); a.Add(f); }
        if (dry) a.Add("--dry-run");
        return Py(a.ToArray());
    }
    public static List<string> StoryCreate(string kind, string[] fields)
    {
        var a = new List<object> { "story.py", "create", kind };
        foreach (var f in fields) { a.Add("-f"); a.Add(f); }
        return Py(a.ToArray());
    }
    public static List<string> StoryDelete(string ids, bool cascade, bool force, bool dry)
    {
        var a = new List<object> { "story.py", "delete", ids, cascade ? "--cascade" : null, force ? "--force" : null, dry ? "--dry-run" : null };
        return Py(a.ToArray());
    }

    // ---- 事件 ----
    // 标签文件走主工具根绝对路径：额外通道若按自身根读，导出的锚点就只剩自动名、
    // 导入也解析不出用户起的标签。
    public static List<string> EventExport(string outFile) =>
        Py("event_export.py", outFile, "--labels", LabelsFile);
    public static List<string> EventImport(string file, bool apply, bool prune, string tol)
    {
        var a = new List<object> { "event_import.py", file, apply ? "--apply" : null, prune ? "--prune" : null };
        if (!string.IsNullOrWhiteSpace(tol)) { a.Add("--tolerance"); a.Add(tol); }
        a.Add("--labels"); a.Add(LabelsFile);
        return Py(a.ToArray());
    }
    public static List<string> EventDsl(string mode, string src, string dst) =>
        Py("event_dsl.py", mode, src, dst);
    public static List<string> LabelsDump() =>
        Py("event_labels.py", "dump", "--labels", LabelsFile);
    public static List<string> LabelsCheck() =>
        Py("event_labels.py", "check", "--labels", LabelsFile);

    /// <summary>标签文件固定放主工具根：额外桥接（通道克隆）也不要把 projects/ 写进通道目录。</summary>
    static string LabelsFile => Path.Combine(Fx.EventDir, "labels.json");

    // ---- 封面 ----
    public static List<string> CoverState() => Py("fxcover.py", "state");
    public static List<string> CoverFromFile(string path, string index)
    {
        var a = new List<object> { "fxcover.py", "from-file", path };
        if (!string.IsNullOrWhiteSpace(index)) { a.Add("--index"); a.Add(index); }
        return Py(a.ToArray());
    }

    // ---- 截图 ----
    public static List<string> ShotShot(string view) => Py("fxshot.py", "shot", view);
    // sweep 的 5 张图 + manifest 直接进主根图库（额外通道的默认目录会落在通道里，图库看不见）
    public static List<string> ShotSweep() => Py("fxshot.py", "sweep", "--out-dir", Fx.ShotsDir);
    public static List<string> ShotLayout() => Py("fxshot.py", "layout");
    public static List<string> ShotRestore() => Py("fxshot.py", "restore");
}

/// <summary>单任务运行器：桥按命令文件串行处理，这里也全局串行，避免互相踩。</summary>
public static class Job
{
    static SemaphoreSlim Gate = new(1, 1);
    public static string Current { get; private set; }
    public static bool Busy => Gate.CurrentCount == 0;

    /// <summary>运行一条命令，逐行回调输出；返回退出码。全局一次只跑一个。</summary>
    public static async Task<int> RunAsync(string label, IReadOnlyList<string> argv, Action<string> onLine, System.Threading.CancellationToken ct = default)
    {
        if (!await Gate.WaitAsync(0))
            throw new InvalidOperationException("已有任务在运行：" + Current);
        Current = label;
        try
        {
            var psi = new ProcessStartInfo(argv[0])
            {
                WorkingDirectory = Fx.Root,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = System.Text.Encoding.UTF8,
                StandardErrorEncoding = System.Text.Encoding.UTF8,
            };
            for (int i = 1; i < argv.Count; i++) psi.ArgumentList.Add(argv[i]);
            psi.Environment["PYTHONIOENCODING"] = "utf-8";
            psi.Environment["PYTHONUNBUFFERED"] = "1";

            using var p = Process.Start(psi);
            p.OutputDataReceived += (_, e) => { if (e.Data != null) onLine(e.Data); };
            p.ErrorDataReceived += (_, e) => { if (e.Data != null) onLine(e.Data); };
            p.BeginOutputReadLine();
            p.BeginErrorReadLine();
            await p.WaitForExitAsync(ct);
            return p.ExitCode;
        }
        finally
        {
            Current = null;
            Gate.Release();
        }
    }
}
