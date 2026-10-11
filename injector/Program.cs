// fuxiang-inject — 把 Python 桥接注入 dwrg.exe 并执行代码。
//
// 原理：neox_engine.dll 导出了完整的 CPython C-API（PyGILState_Ensure /
// PyRun_SimpleString / PyGILState_Release）。因此无需编写原生 DLL：
// 在目标进程内分配一段 shellcode，依次调用这三个导出函数即可执行任意 Python。
//
// 用法：
//   fuxiang-inject install            安装常驻桥接（幂等）
//   fuxiang-inject status             查看桥接状态
//   fuxiang-inject exec <file.py>     在游戏内执行一个 Python 文件
//   fuxiang-inject eval "<py>"        在游戏内执行一段 Python
//   fuxiang-inject selftest           离线自检（不碰游戏）
//   fuxiang-inject raw <file.py>      直接注入（不经过桥接）
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

namespace Fuxiang
{
    internal static class Native
    {
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern IntPtr LoadLibraryExW(string path, IntPtr hFile, uint flags);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr GetProcAddress(IntPtr h, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr h);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr VirtualAllocEx(IntPtr proc, IntPtr addr, IntPtr size, uint type, uint protect);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool VirtualFreeEx(IntPtr proc, IntPtr addr, IntPtr size, uint type);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool WriteProcessMemory(IntPtr proc, IntPtr addr, byte[] buf, IntPtr size, out IntPtr written);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr CreateRemoteThread(IntPtr proc, IntPtr attr, IntPtr stack, IntPtr start, IntPtr param, uint flags, out int tid);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint WaitForSingleObject(IntPtr h, uint ms);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool GetExitCodeThread(IntPtr h, out uint code);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetDllDirectoryW(string path);
    }

    internal static class Program
    {
        const uint PROCESS_ALL_ACCESS = 0x1F0FFF;
        const uint MEM_COMMIT_RESERVE = 0x3000;
        const uint PAGE_EXECUTE_READWRITE = 0x40;
        const uint PAGE_READWRITE = 0x04;
        const uint MEM_RELEASE = 0x8000;

        // 2026-10-03 起两个根分离：工具链不再住在游戏安装目录里（反作弊对自家安装目录
        // 做异物扫描是成本最低的检测，tools/ + research/ 整树都在旁边等于自报家门）。
        // 游戏根只用于找 neox_engine.dll / dwrg 进程；工具根放桥、ipc、导出表。
        // 均可用环境变量覆盖：FUXIANG_GAME_DIR / FUXIANG_TOOL_ROOT。
        // 工具根另有自动推导：从 exe 自身位置向上找 bridge/fuxiang_bridge.py（找到即采用，
        // 环境变量优先）——整树搬目录后无需重编译。游戏根无法从工具侧推导（那是游戏装的
        // 位置），保留默认值 + 环境变量；install 运行时会优先取目标进程 neox_engine.dll 的
        // 实际模块路径，因此默认值只在离线 verify-exports/selftest 兜底。
        static string Env(string k, string def)
        {
            var v = Environment.GetEnvironmentVariable(k);
            return string.IsNullOrEmpty(v) ? def : v;
        }
        static string GameInstallDir => Env("FUXIANG_GAME_DIR", ".");
        static string ToolRoot
        {
            get
            {
                var env = Environment.GetEnvironmentVariable("FUXIANG_TOOL_ROOT");
                if (!string.IsNullOrEmpty(env)) return env;
                try
                {
                    // 单文件发布下 Assembly.Location 为空，优先用 ProcessPath（exe 自身路径）
                    var exe = Environment.ProcessPath;
                    if (string.IsNullOrEmpty(exe)) exe = System.Reflection.Assembly.GetEntryAssembly()!.Location;
                    var dir = Path.GetDirectoryName(Path.GetFullPath(exe));
                    for (int i = 0; i < 8 && dir is not null; i++)
                    {
                        if (File.Exists(Path.Combine(dir, "bridge", "fuxiang_bridge.py"))) return dir;
                        dir = Path.GetDirectoryName(dir);
                    }
                }
                catch { }
                return AppContext.BaseDirectory;
            }
        }
        static string EngDll => Path.Combine(GameInstallDir, "neox_engine.dll");
        static string ExportsFile => Path.Combine(ToolRoot, "research", "tools_extract", "neox_exports.txt");
        static string FxRoot => ToolRoot;
        static string BridgePy => Path.Combine(FxRoot, "bridge", "fuxiang_bridge.py");
        static string IpcDir => Path.Combine(FxRoot, "ipc");
        static string StateFile => Path.Combine(IpcDir, "bridge_state.json");
        static string CmdDir => Path.Combine(IpcDir, "cmd");
        static string OutDir => Path.Combine(IpcDir, "out");

        static int Main(string[] args)
        {
            Console.OutputEncoding = Encoding.UTF8;
            if (args.Length == 0) { Usage(); return 2; }
            try
            {
                switch (args[0])
                {
                    case "install": return Install();
                    case "status": return Status();
                    case "exec": return ExecFile(args.Length > 1 ? args[1] : null);
                    case "eval": return Eval(args.Length > 1 ? args[1] : null);
                    case "raw": return Raw(args.Length > 1 ? args[1] : null);
                    case "selftest": return SelfTest();
                    case "verify-exports": return VerifyExportsCmd(args);
                    default: Usage(); return 2;
                }
            }
            catch (Exception e)
            {
                Console.Error.WriteLine("ERROR: " + e.Message);
                return 1;
            }
        }

        static void Usage()
        {
            Console.WriteLine("fuxiang-inject <命令>");
            Console.WriteLine("  install            安装常驻桥接（需游戏已运行）");
            Console.WriteLine("  status             查看桥接状态");
            Console.WriteLine("  exec <file.py>     经桥接在游戏内执行 Python 文件");
            Console.WriteLine("  eval \"<py>\"        经桥接在游戏内执行 Python 代码");
            Console.WriteLine("  raw  <file.py>     直接注入执行（不经过桥接）");
            Console.WriteLine("  verify-exports     核对导出表与当前 neox_engine.dll（离线只读；可带 <dll> <exports.txt> 自定路径）");
            Console.WriteLine("  selftest           离线自检");
        }

        // ---------------- exports ----------------
        // 注入只真正用这三个函数；它们的 RVA 错一个，stub 一跑就是 0xc0000005。
        static readonly string[] CriticalExports = { "PyGILState_Ensure", "PyRun_SimpleString", "PyGILState_Release" };

        static ulong RvaOf(string name)
        {
            foreach (var line in File.ReadLines(ExportsFile))
            {
                int t = line.IndexOf('\t');
                if (t < 0) continue;
                if (line.Substring(0, t) == name)
                {
                    var hex = line.Substring(t + 1).Trim();
                    return Convert.ToUInt64(hex.StartsWith("0x") ? hex.Substring(2) : hex, 16);
                }
            }
            throw new Exception("export not found: " + name);
        }

        // ---------------- PE 导出表校验（防崩溃的第一道闸） ----------------
        // neox_exports.txt 是从某个引擎版本的 neox_engine.dll 提取的快照。引擎一更新，
        // 表里的 RVA 全部作废，按旧表注入 = 错地址调用 = 游戏必崩；崩出来的 dump 带
        // 整份进程内存（桥的模块名/路径全在里面）。所以在 OpenProcess 之前先对着
        // **磁盘上的 DLL** 把导出表核一遍：关键 3 项失配就干净拒绝，宁可不装桥。

        /// <summary>读 DLL 文件的 PE 导出目录，返回 {导出名: RVA}（只读文件，不碰游戏进程）。</summary>
        static Dictionary<string, ulong> ReadDllExportTable(string dllPath)
        {
            byte[] pe = File.ReadAllBytes(dllPath);
            if (pe.Length < 0x40 || pe[0] != 'M' || pe[1] != 'Z')
                throw new Exception("不是 PE 文件: " + dllPath);
            uint peOff = BitConverter.ToUInt32(pe, 0x3C);
            if (peOff + 4 + 20 > (uint)pe.Length || pe[peOff] != 'P' || pe[peOff + 1] != 'E' || pe[peOff + 2] != 0 || pe[peOff + 3] != 0)
                throw new Exception("PE 签名缺失: " + dllPath);
            ushort machine = BitConverter.ToUInt16(pe, (int)peOff + 4);
            if (machine != 0x8664) throw new Exception("非 x64 PE（machine=0x" + machine.ToString("x") + "）");
            ushort numSections = BitConverter.ToUInt16(pe, (int)peOff + 6);
            ushort sizeOpt = BitConverter.ToUInt16(pe, (int)peOff + 20);
            int opt = (int)peOff + 24;
            ushort magic = BitConverter.ToUInt16(pe, opt);
            int ddOff;                                   // 数据目录数组起点
            if (magic == 0x20B) ddOff = opt + 112;       // PE32+
            else if (magic == 0x10B) ddOff = opt + 96;   // PE32
            else throw new Exception("未知 OptionalHeader magic=0x" + magic.ToString("x"));
            uint expRva = BitConverter.ToUInt32(pe, ddOff);
            uint expSize = BitConverter.ToUInt32(pe, ddOff + 4);
            if (expRva == 0) throw new Exception("DLL 没有导出表: " + dllPath);

            // RVA → 文件偏移（按节表映射）
            int sec = opt + sizeOpt;
            Func<ulong, uint> rva2off = rva =>
            {
                for (int i = 0; i < numSections; i++)
                {
                    int s = sec + i * 40;
                    uint vsize = BitConverter.ToUInt32(pe, s + 8);
                    uint va = BitConverter.ToUInt32(pe, s + 12);
                    uint rawsize = BitConverter.ToUInt32(pe, s + 16);
                    uint raw = BitConverter.ToUInt32(pe, s + 20);
                    uint span = vsize > rawsize ? vsize : rawsize;
                    if (rva >= va && rva < va + span)
                        return rawsize == 0 ? 0 : (uint)(rva - va + raw);
                }
                return (uint)rva;   // 头部区间
            };

            uint e = rva2off(expRva);
            int numNames = (int)BitConverter.ToUInt32(pe, (int)e + 0x14);
            uint offFunc = rva2off(BitConverter.ToUInt32(pe, (int)e + 0x1C));
            uint offNames = rva2off(BitConverter.ToUInt32(pe, (int)e + 0x20));
            uint offOrds = rva2off(BitConverter.ToUInt32(pe, (int)e + 0x24));

            var table = new Dictionary<string, ulong>(numNames);
            for (int i = 0; i < numNames; i++)
            {
                uint nameRva = BitConverter.ToUInt32(pe, (int)offNames + i * 4);
                int no = (int)rva2off(nameRva);
                int end = no;
                while (end < pe.Length && pe[end] != 0) end++;
                string name = Encoding.ASCII.GetString(pe, no, end - no);
                ushort ord = BitConverter.ToUInt16(pe, (int)offOrds + i * 2);
                ulong rva = BitConverter.ToUInt32(pe, (int)offFunc + ord * 4);
                table[name] = rva;
            }
            if (table.Count == 0) throw new Exception("导出表解析出 0 个名字（expSize=" + expSize + "）");
            return table;
        }

        sealed class ExportCheck
        {
            public int Checked;        // 表里成功比对的条目数
            public int Matched;
            public readonly List<string> CriticalProblems = new List<string>();   // 关键 3 项：缺失/失配 ⇒ 禁止注入
            public readonly List<string> Advisory = new List<string>();           // 非关键漂移：表过期征兆
            public bool Ok { get { return CriticalProblems.Count == 0; } }
        }

        static ExportCheck VerifyExports(string dllPath, string exportsFile)
        {
            var table = ReadDllExportTable(dllPath);
            var chk = new ExportCheck();
            foreach (var line in File.ReadLines(exportsFile))
            {
                int t = line.IndexOf('\t');
                if (t <= 0) continue;
                string name = line.Substring(0, t);
                ulong want;
                try
                {
                    var hex = line.Substring(t + 1).Trim();
                    want = Convert.ToUInt64(hex.StartsWith("0x") ? hex.Substring(2) : hex, 16);
                }
                catch { continue; }                  // 解析不了的行不算失配
                chk.Checked++;
                bool critical = Array.IndexOf(CriticalExports, name) >= 0;
                if (!table.TryGetValue(name, out ulong have))
                {
                    var msg = name + " 在当前 DLL 导出表里不存在";
                    if (critical) chk.CriticalProblems.Add(msg); else chk.Advisory.Add(msg);
                    continue;
                }
                if (have == want) { chk.Matched++; continue; }
                var diff = name + " RVA 表=0x" + want.ToString("x") + " 实际=0x" + have.ToString("x");
                if (critical) chk.CriticalProblems.Add(diff); else chk.Advisory.Add(diff);
            }
            foreach (var name in CriticalExports)
                if (!File.ReadLines(exportsFile).Any(l => l.StartsWith(name + "\t")))
                    chk.CriticalProblems.Add(name + " 不在导出表文件里（注入根本取不到地址）");
            return chk;
        }

        /// <summary>install/raw 注入前的统一闸门：关键项失配就拒绝（返回 null = 放行）。
        /// dllPath 优先取目标进程 neox_engine.dll 的实际模块路径（进程可能在任何目录）。</summary>
        static ExportCheck GateOrReport(string dllPath)
        {
            var chk = VerifyExports(dllPath, ExportsFile);
            if (chk.Advisory.Count > 0)
                Console.WriteLine("⚠️ 导出表与当前引擎有 " + chk.Advisory.Count + " 处非关键漂移（表可能过期；"
                                  + "关键 " + CriticalExports.Length + " 项仍匹配）。建议重新生成 neox_exports.txt。示例: "
                                  + string.Join("; ", chk.Advisory.Take(3)));
            if (chk.Ok)
            {
                Console.WriteLine("导出表校验 ✓ " + chk.Matched + "/" + chk.Checked + " 项与 neox_engine.dll 一致"
                                  + "（关键 " + CriticalExports.Length + " 项已核对）");
                return chk;
            }
            Console.Error.WriteLine("⛔ 导出表校验失败，**拒绝注入**（防崩闸门）：");
            foreach (var p in chk.CriticalProblems.Take(6))
                Console.Error.WriteLine("   - " + p);
            Console.Error.WriteLine("   引擎很可能已更新。按旧表注入 = 错地址调用 = 游戏必崩（0xc0000005），");
            Console.Error.WriteLine("   崩溃报告会带走整份进程内存。请先重新生成 research/tools_extract/neox_exports.txt。");
            return null;
        }


        // ---------------- x64 stub ----------------
        static byte[] BuildStub(ulong ensure, ulong run, ulong release, ulong codePtr)
        {
            var ms = new MemoryStream();
            void B(params byte[] b) => ms.Write(b, 0, b.Length);
            void MovRax(ulong v) { B(0x48, 0xB8); ms.Write(BitConverter.GetBytes(v), 0, 8); }
            void MovRcx(ulong v) { B(0x48, 0xB9); ms.Write(BitConverter.GetBytes(v), 0, 8); }
            B(0x48, 0x83, 0xEC, 0x28);
            MovRax(ensure); B(0xFF, 0xD0);
            B(0x48, 0x89, 0x44, 0x24, 0x20);
            MovRcx(codePtr);
            MovRax(run); B(0xFF, 0xD0);
            B(0x48, 0x8B, 0x4C, 0x24, 0x20);
            MovRax(release); B(0xFF, 0xD0);
            B(0x48, 0x83, 0xC4, 0x28);
            B(0x31, 0xC0);
            B(0xC3);
            return ms.ToArray();
        }

        // ---------------- process discovery ----------------
        static int FindGamePid()
        {
            foreach (var p in Process.GetProcessesByName("dwrg"))
            {
                try
                {
                    foreach (ProcessModule m in p.Modules)
                        if (string.Equals(m.ModuleName, "neox_engine.dll", StringComparison.OrdinalIgnoreCase))
                            return p.Id;
                }
                catch { }
            }
            return 0;
        }

        /// <summary>目标进程内 neox_engine.dll 的实际文件路径（游戏装在哪个目录都能取到；
        /// 取不到返回 null，调用方回落 FUXIANG_GAME_DIR 默认值）。</summary>
        static string GameDllPathOf(int pid)
        {
            try
            {
                foreach (ProcessModule m in Process.GetProcessById(pid).Modules)
                    if (string.Equals(m.ModuleName, "neox_engine.dll", StringComparison.OrdinalIgnoreCase))
                        return m.FileName;
            }
            catch { }
            return null;
        }

        // ---------------- injection core ----------------
        /// <summary>在目标进程内执行一段 Python 源码。返回 (ok, message)。</summary>
        static (bool, string) InjectPython(int pid, string pythonSource, int timeoutMs = 120000)
        {
            ulong rvaEnsure = RvaOf("PyGILState_Ensure");
            ulong rvaRun = RvaOf("PyRun_SimpleString");
            ulong rvaRelease = RvaOf("PyGILState_Release");

            IntPtr proc = Native.OpenProcess(PROCESS_ALL_ACCESS, false, pid);
            if (proc == IntPtr.Zero)
                return (false, "OpenProcess 失败 (错误 " + Marshal.GetLastWin32Error() + ")。请以管理员身份运行。");
            try
            {
                ulong baseAddr = 0;
                foreach (ProcessModule m in Process.GetProcessById(pid).Modules)
                    if (string.Equals(m.ModuleName, "neox_engine.dll", StringComparison.OrdinalIgnoreCase))
                        baseAddr = (ulong)m.BaseAddress.ToInt64();
                if (baseAddr == 0) return (false, "目标进程未加载 neox_engine.dll");

                byte[] codeBytes = Encoding.UTF8.GetBytes(pythonSource);
                var codeWithNul = new byte[codeBytes.Length + 1];
                Array.Copy(codeBytes, codeWithNul, codeBytes.Length);

                IntPtr remoteCode = Native.VirtualAllocEx(proc, IntPtr.Zero, (IntPtr)codeWithNul.Length,
                                                          MEM_COMMIT_RESERVE, PAGE_READWRITE);
                if (remoteCode == IntPtr.Zero) return (false, "VirtualAllocEx(code) 失败 " + Marshal.GetLastWin32Error());
                IntPtr w;
                Native.WriteProcessMemory(proc, remoteCode, codeWithNul, (IntPtr)codeWithNul.Length, out w);

                byte[] stub = BuildStub(baseAddr + rvaEnsure, baseAddr + rvaRun, baseAddr + rvaRelease,
                                        (ulong)remoteCode.ToInt64());
                IntPtr remoteStub = Native.VirtualAllocEx(proc, IntPtr.Zero, (IntPtr)stub.Length,
                                                          MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE);
                if (remoteStub == IntPtr.Zero) return (false, "VirtualAllocEx(stub) 失败 " + Marshal.GetLastWin32Error());
                Native.WriteProcessMemory(proc, remoteStub, stub, (IntPtr)stub.Length, out w);

                int tid;
                IntPtr th = Native.CreateRemoteThread(proc, IntPtr.Zero, IntPtr.Zero, remoteStub,
                                                      IntPtr.Zero, 0, out tid);
                if (th == IntPtr.Zero) return (false, "CreateRemoteThread 失败 " + Marshal.GetLastWin32Error());

                uint wr = Native.WaitForSingleObject(th, (uint)timeoutMs);
                if (wr != 0)
                {
                    // 线程还卡在 PyRun_SimpleString 里。此时 VirtualFreeEx = 在活线程脚下拆地板，
                    // 必崩（0xc0000005）且崩溃 dump 会把进程内存整个带走 —— 宁可泄漏两小页，也不制造崩溃。
                    Native.CloseHandle(th);
                    return (false, "等待注入线程超时（" + timeoutMs + "ms）；远程内存暂留不释放（防崩，重启游戏即回收）");
                }
                uint exit; Native.GetExitCodeThread(th, out exit);
                Native.CloseHandle(th);
                Native.VirtualFreeEx(proc, remoteStub, IntPtr.Zero, MEM_RELEASE);
                Native.VirtualFreeEx(proc, remoteCode, IntPtr.Zero, MEM_RELEASE);
                return (true, "ok (tid=" + tid + ", exit=0x" + exit.ToString("x") + ")");
            }
            finally { Native.CloseHandle(proc); }
        }

        // ---------------- install ----------------
        // 多进程支持（2026-10-01）：本机可能同时跑两个 dwrg（另一个会话的桥已装好）。
        // 在主 ipc 目录放 install_override.json 可指定目标：
        //   {"pid": 15088, "bridge": "D:\\...\\tools\\fuxiang2\\bridge\\fuxiang_bridge.py",
        //    "ipc": "D:\\...\\tools\\fuxiang2\\ipc"}
        // 三键均可选；注入成功后该文件自动删除，避免残留覆盖把下次别人的安装带偏。
        static int Install()
        {
            string bridgePy = BridgePy;
            string ipcDir = IpcDir;
            string stateFile = StateFile;
            int pid = 0;

            string overrideFile = Path.Combine(IpcDir, "install_override.json");
            if (File.Exists(overrideFile))
            {
                try
                {
                    using var doc = System.Text.Json.JsonDocument.Parse(File.ReadAllText(overrideFile));
                    var o = doc.RootElement;
                    if (o.TryGetProperty("pid", out var pe) && pe.TryGetInt32(out int op_) && op_ > 0) pid = op_;
                    if (o.TryGetProperty("bridge", out var be) && be.GetString() is string bs && bs.Length > 0) bridgePy = bs;
                    if (o.TryGetProperty("ipc", out var ie) && ie.GetString() is string is_ && is_.Length > 0)
                    {
                        ipcDir = is_;
                        stateFile = Path.Combine(ipcDir, "bridge_state.json");
                    }
                    Console.WriteLine("使用覆盖配置 " + overrideFile + "：pid=" + (pid > 0 ? pid.ToString() : "自动") + " bridge=" + bridgePy + " ipc=" + ipcDir);
                }
                catch (Exception e)
                {
                    Console.Error.WriteLine("覆盖配置解析失败（忽略，按默认目标安装）: " + e.Message);
                }
            }

            string cmdDir = Path.Combine(ipcDir, "cmd");
            string outDir = Path.Combine(ipcDir, "out");
            Directory.CreateDirectory(ipcDir);
            Directory.CreateDirectory(cmdDir);
            Directory.CreateDirectory(outDir);

            if (pid == 0) pid = FindGamePid();
            if (pid == 0)
            {
                Console.Error.WriteLine("未找到运行中的《第五人格》(dwrg.exe)。请先启动并登录游戏。");
                return 3;
            }
            if (!File.Exists(bridgePy))
            {
                Console.Error.WriteLine("缺少桥接脚本: " + bridgePy);
                return 4;
            }
            if (GateOrReport(GameDllPathOf(pid) ?? EngDll) == null) return 9;

            // co_filename 用诱饵名 + optimize=2（剥 docstring/assert）：桥模块在游戏内存里的
            // 代码对象不再携带真实磁盘路径。⚠️ 路径本身仍会以 __file__/sys.path 的形态驻留
            // （模块按路径装载是机制本身），见 INJECTOR_USAGE.md「防崩与残留边界」。
            // ⚠️ __file__ 必须在两个分支都刷新（2026-10-04 实机教训）：重注入走的是
            // 「模块已存在」分支，旧实现不在那里刷新 __file__ → 模块内部按它推导的
            // ROOT/CMD_DIR/STATE_FILE 全部指向旧位置，新代码旧路径，重注入等于白做。
            string bootstrap =
                "import sys, types, traceback\n" +
                "try:\n" +
                "    _p = r'" + bridgePy + "'\n" +
                "    _m = sys.modules.get('fuxiang_bridge')\n" +
                "    if _m is None:\n" +
                "        _m = types.ModuleType('fuxiang_bridge')\n" +
                "        sys.modules['fuxiang_bridge'] = _m\n" +
                "    _m.__file__ = _p\n" +
                "    exec(compile(open(_p, 'rb').read(), '<ugc>', 'exec', 0, 0, 2), _m.__dict__)\n" +
                "except Exception:\n" +
                "    open(r'" + ipcDir + "\\_bootstrap_err.txt', 'w', encoding='utf-8').write(traceback.format_exc())\n";

            Console.WriteLine("找到游戏进程 PID=" + pid + "，正在安装桥接...");
            var (ok, msg) = InjectPython(pid, bootstrap);
            if (ok)
            {
                try { File.Delete(overrideFile); } catch { }
            }
            else { Console.Error.WriteLine("注入失败: " + msg); return 5; }
            Console.WriteLine("注入完成: " + msg);

            // 等待心跳
            for (int i = 0; i < 40; i++)
            {
                System.Threading.Thread.Sleep(150);
                if (File.Exists(stateFile) && FreshStateFile(stateFile, 3.0)) break;
            }
            string errFile = Path.Combine(ipcDir, "_bootstrap_err.txt");
            if (File.Exists(errFile))
            {
                Console.Error.WriteLine("桥接加载报错:");
                Console.Error.WriteLine(File.ReadAllText(errFile));
                return 6;
            }
            if (!FreshStateFile(stateFile, 5.0)) { Console.Error.WriteLine("桥接未上报心跳，安装可能失败（详见 " + ipcDir + "\\bridge.log）"); return 7; }

            Console.WriteLine("桥接已就绪 ✓");
            PrintState();
            return 0;
        }

        static bool FreshState(double maxAgeSec) => FreshStateFile(StateFile, maxAgeSec);

        static bool FreshStateFile(string statePath, double maxAgeSec)
        {
            try
            {
                var txt = File.ReadAllText(statePath);
                var m = Regex.Match(txt, "\"now\"\\s*:\\s*([0-9.]+)");
                if (!m.Success) return false;
                double now = double.Parse(m.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture);
                double age = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - now;
                return age <= maxAgeSec;
            }
            catch { return false; }
        }

        static void PrintState()
        {
            try { Console.WriteLine(File.ReadAllText(StateFile).Trim()); } catch { }
        }

        static int Status()
        {
            int pid = FindGamePid();
            Console.WriteLine("游戏进程: " + (pid == 0 ? "未运行" : "PID=" + pid));
            if (!File.Exists(StateFile)) { Console.WriteLine("桥接状态: 未安装"); return 1; }
            bool fresh = FreshState(3.0);
            Console.WriteLine("桥接状态: " + (fresh ? "运行中 ✓" : "已失联（心跳过期）"));
            PrintState();
            return fresh ? 0 : 1;
        }

        // ---------------- bridge-based command ----------------
        static int SendCommand(string op, string argsJson)
        {
            int pid = FindGamePid();
            if (pid == 0) { Console.Error.WriteLine("游戏未运行"); return 3; }
            if (!FreshState(3.0))
            {
                Console.Error.WriteLine("桥接未运行，正在自动安装...");
                int rc = Install();
                if (rc != 0) return rc;
            }

            string id = "c" + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() + "_" + new Random().Next(1000, 9999);
            string cmdFile = Path.Combine(CmdDir, id + ".json");
            string outFile = Path.Combine(OutDir, id + ".json");
            string json = "{\"id\":\"" + id + "\",\"op\":\"" + op + "\",\"args\":" + argsJson + "}";
            File.WriteAllText(cmdFile, json, new UTF8Encoding(false));

            for (int i = 0; i < 1200; i++)   // ~120s
            {
                System.Threading.Thread.Sleep(100);
                if (File.Exists(outFile))
                {
                    Console.WriteLine(File.ReadAllText(outFile));
                    try { File.Delete(outFile); } catch { }
                    return 0;
                }
            }
            Console.Error.WriteLine("等待结果超时（命令文件: " + cmdFile + "）");
            return 8;
        }

        static int ExecFile(string file)
        {
            if (file == null || !File.Exists(file)) { Console.Error.WriteLine("找不到文件: " + file); return 2; }
            string code = File.ReadAllText(file);
            return SendCommand("eval", "{\"code\":" + JsonStr(code) + "}");
        }

        static int Eval(string code)
        {
            if (string.IsNullOrEmpty(code)) { Console.Error.WriteLine("缺少代码"); return 2; }
            return SendCommand("eval", "{\"code\":" + JsonStr(code) + "}");
        }

        static int Raw(string file)
        {
            if (file == null || !File.Exists(file)) { Console.Error.WriteLine("找不到文件: " + file); return 2; }
            int pid = FindGamePid();
            if (pid == 0) { Console.Error.WriteLine("游戏未运行"); return 3; }
            if (GateOrReport(GameDllPathOf(pid) ?? EngDll) == null) return 9;
            var (ok, msg) = InjectPython(pid, File.ReadAllText(file));
            Console.WriteLine(ok ? "注入执行完成: " + msg : "注入失败: " + msg);
            return ok ? 0 : 5;
        }

        static string JsonStr(string s)
        {
            var sb = new StringBuilder("\"");
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            return sb.Append('"').ToString();
        }

        static int VerifyExportsCmd(string[] args)
        {
            bool noArg = args.Length <= 1;
            string dll = noArg ? EngDll : args[1];
            string exp = args.Length > 2 ? args[2] : ExportsFile;
            if (!File.Exists(dll))
            {
                Console.WriteLine("未找到引擎 DLL：" + Path.GetFullPath(dll));
                Console.WriteLine("离线核对请显式给出路径：fuxiang-inject verify-exports <neox_engine.dll 路径> [导出表路径]");
                Console.WriteLine("（或设置环境变量 FUXIANG_GAME_DIR 指向游戏安装目录；install 时会自动取目标进程的模块路径，无需设置）");
                return 2;
            }
            Console.WriteLine("校验导出表（离线只读，不碰游戏进程）：");
            Console.WriteLine("  DLL    " + dll);
            Console.WriteLine("  导出表 " + exp);
            var chk = VerifyExports(dll, exp);
            Console.WriteLine("  比对 " + chk.Checked + " 项：一致 " + chk.Matched
                              + "，关键项问题 " + chk.CriticalProblems.Count
                              + "，非关键漂移 " + chk.Advisory.Count);
            foreach (var p in chk.CriticalProblems.Take(6)) Console.WriteLine("  ⛔ " + p);
            foreach (var p in chk.Advisory.Take(6)) Console.WriteLine("  ⚠️ " + p);
            if (chk.Ok) { Console.WriteLine("结论：关键导出全部匹配，可以安全注入 ✓"); return 0; }
            Console.WriteLine("结论：禁止注入（关键导出失配 —— 引擎多半更新了，先重新生成导出表）");
            return 1;
        }

        // ---------------- selftest ----------------
        static int SelfTest()
        {
            Console.WriteLine("自检 1/3: 导出表加载...");
            Console.WriteLine("  PyGILState_Ensure  RVA=0x" + RvaOf("PyGILState_Ensure").ToString("x"));
            Console.WriteLine("  PyRun_SimpleString RVA=0x" + RvaOf("PyRun_SimpleString").ToString("x"));
            Console.WriteLine("  PyGILState_Release RVA=0x" + RvaOf("PyGILState_Release").ToString("x"));

            Console.WriteLine("自检 2/3: stub 字节码...");
            var stub = BuildStub(0x1111111111111111, 0x2222222222222222, 0x3333333333333333, 0x4444444444444444);
            Console.WriteLine("  stub " + stub.Length + " 字节: " + BitConverter.ToString(stub).Replace("-", " "));

            Console.WriteLine("自检 3/3: 导出表 vs 引擎 DLL（离线只读）...");
            try
            {
                var chk = VerifyExports(EngDll, ExportsFile);
                Console.WriteLine("  比对 " + chk.Checked + " 项：一致 " + chk.Matched
                                  + "，关键问题 " + chk.CriticalProblems.Count
                                  + "，非关键漂移 " + chk.Advisory.Count);
                if (!chk.Ok)
                {
                    Console.WriteLine("⛔ 关键导出失配 —— **当前禁止注入**（注入必崩）：");
                    foreach (var p in chk.CriticalProblems) Console.WriteLine("  - " + p);
                    return 1;
                }
            }
            catch (Exception e)
            {
                Console.WriteLine("  （跳过：读不到 DLL 或表：" + e.Message + "）");
            }
            Console.WriteLine("自检通过 ✓");
            return 0;
        }
    }
}
