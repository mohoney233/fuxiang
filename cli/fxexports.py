#!/usr/bin/env python3
"""fxexports — 重建 / 核对 neox_engine.dll 的 PE 导出表（纯标准库，无第三方依赖）。

导出表 research/tools_extract/neox_exports.txt 是注入器防崩闸门的真源：注入只真正用
PyGILState_Ensure / PyRun_SimpleString / PyGILState_Release 三个函数的 RVA，引擎一更新
表全部作废，按旧表注入 = 错地址调用 = 游戏必崩（0xc0000005）。本脚本与
research/tools_extract/pe_exports.js（Node 版）、注入器 verify-exports（C# 版）三者的
解析逻辑与判定口径保持一致；控制台的「重建导出表」按钮走的就是这里。

用法：
  python cli/fxexports.py build  <neox_engine.dll> [--out <表路径>]   # 重建（旧表自动备份 .bak）
  python cli/fxexports.py verify <neox_engine.dll> [--table <表路径>] # 核对（关键 3 项失配 = exit 9）
  python cli/fxexports.py show   [--table <表路径>]                   # 只看关键 3 项与条目数

退出码：0 = 通过；9 = 核对失败（与注入器防崩闸门同一口径）；2 = 用法/文件错误。
"""
import argparse
import os
import shutil
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TABLE = os.path.join(ROOT, "research", "tools_extract", "neox_exports.txt")
CRITICAL = ["PyGILState_Ensure", "PyRun_SimpleString", "PyGILState_Release"]


def read_dll_exports(dll_path):
    """读 DLL 的 PE 导出目录，返回 [(名字, RVA), ...]（按导出名称表枚举序）。

    与 injector/Program.cs::ReadDllExportTable、pe_exports.js 同一套 PE 逻辑：
    x64 检查、PE32+/PE32 数据目录偏移、节表 RVA→文件偏移（span = max(vsize, rawsize)）。
    """
    with open(dll_path, "rb") as f:
        pe = f.read()
    if len(pe) < 0x40 or pe[0:2] != b"MZ":
        raise ValueError("不是 PE 文件: %s" % dll_path)
    (pe_off,) = struct.unpack_from("<I", pe, 0x3C)
    if pe_off + 24 > len(pe) or pe[pe_off:pe_off + 4] != b"PE\0\0":
        raise ValueError("PE 签名缺失: %s" % dll_path)
    (machine,) = struct.unpack_from("<H", pe, pe_off + 4)
    if machine != 0x8664:
        raise ValueError("非 x64 PE（machine=0x%x）" % machine)
    (num_sections,) = struct.unpack_from("<H", pe, pe_off + 6)
    (size_opt,) = struct.unpack_from("<H", pe, pe_off + 20)
    opt = pe_off + 24
    (magic,) = struct.unpack_from("<H", pe, opt)
    if magic == 0x20B:      # PE32+
        dd_off = opt + 112
    elif magic == 0x10B:    # PE32
        dd_off = opt + 96
    else:
        raise ValueError("未知 OptionalHeader magic=0x%x" % magic)
    (exp_rva, exp_size) = struct.unpack_from("<II", pe, dd_off)
    if exp_rva == 0:
        raise ValueError("DLL 没有导出表: %s" % dll_path)

    sec = opt + size_opt

    def rva2off(rva):
        for i in range(num_sections):
            s = sec + i * 40
            # IMAGE_SECTION_HEADER: +8 VirtualSize, +12 VirtualAddress,
            # +16 SizeOfRawData, +20 PointerToRawData
            vsize, va, rawsize, raw = struct.unpack_from("<IIII", pe, s + 8)
            span = max(vsize, rawsize)
            if va <= rva < va + span:
                return 0 if rawsize == 0 else rva - va + raw
        return rva  # 头部区间

    e = rva2off(exp_rva)
    # IMAGE_EXPORT_DIRECTORY（40 字节）:
    # 0x00 Characteristics, 0x04 TimeDateStamp, 0x08/0x0A Version,
    # 0x0C Name, 0x10 Base, 0x14 NumberOfFunctions, 0x18 NumberOfNames,
    # 0x1C/0x20/0x24 三个数组 RVA
    (_chars, _ts, _vmaj, _vmin, _name_rva, _base,
     n_funcs, n_names, off_funcs, off_names, off_ords) = struct.unpack_from(
        "<IIHHIIIIIII", pe, e)
    off_funcs = rva2off(off_funcs)
    off_names = rva2off(off_names)
    off_ords = rva2off(off_ords)

    out = []
    for i in range(n_names):
        (name_rva,) = struct.unpack_from("<I", pe, off_names + i * 4)
        no = rva2off(name_rva)
        end = pe.index(b"\0", no)
        name = pe[no:end].decode("latin1")
        (ordn,) = struct.unpack_from("<H", pe, off_ords + i * 2)
        (rva,) = struct.unpack_from("<I", pe, off_funcs + ordn * 4)
        out.append((name, rva))
    if not out:
        raise ValueError("导出表解析出 0 个名字（expSize=%d）" % exp_size)
    return out


def load_table(path):
    """读导出表文件 → {名字: RVA}；解析不了的行跳过（与注入器同口径）。"""
    table = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            t = line.find("\t")
            if t <= 0:
                continue
            hexpart = line[t + 1:].strip()
            try:
                table[line[:t]] = int(hexpart[2:] if hexpart.startswith("0x") else hexpart, 16)
            except ValueError:
                continue
    return table


def cmd_build(args):
    entries = read_dll_exports(args.dll)
    out = os.path.abspath(args.out or DEFAULT_TABLE)
    text = "".join("%s\t0x%x\n" % (n, r) for n, r in entries)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        with open(out, "r", encoding="utf-8", errors="replace") as f:
            if f.read() == text:
                print("导出表已是最新（%d 项）：%s" % (len(entries), out))
                return 0
        bak = out + ".bak"
        shutil.copy2(out, bak)
        print("旧表已备份 → %s" % bak)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("已重建导出表：%d 项 → %s" % (len(entries), out))
    byname = dict(entries)
    missing = [n for n in CRITICAL if n not in byname]
    for n in CRITICAL:
        if n in byname:
            print("  %s\t0x%x" % (n, byname[n]))
    if missing:
        print("⚠️ 关键函数缺失：%s（该引擎无法注入）" % ", ".join(missing), file=sys.stderr)
        return 9
    print("下一步核对：python cli/fxexports.py verify <dll>（或直接装桥，注入器会自动核对）")
    return 0


def cmd_verify(args):
    table_path = os.path.abspath(args.table or DEFAULT_TABLE)
    if not os.path.exists(table_path):
        print("⛔ 导出表文件不存在：%s（先 build 重建）" % table_path, file=sys.stderr)
        return 9
    try:
        dll_table = dict(read_dll_exports(args.dll))
    except ValueError as e:
        print("⛔ %s" % e, file=sys.stderr)
        return 9
    want = load_table(table_path)
    problems, advisory, matched, checked = [], [], 0, 0
    for name, rva in want.items():
        checked += 1
        critical = name in CRITICAL
        if name not in dll_table:
            (problems if critical else advisory).append("%s 在当前 DLL 导出表里不存在" % name)
        elif dll_table[name] != rva:
            (problems if critical else advisory).append(
                "%s RVA 表=0x%x 实际=0x%x" % (name, rva, dll_table[name]))
        else:
            matched += 1
    for n in CRITICAL:
        if n not in want:
            problems.append("%s 不在导出表文件里（注入根本取不到地址）" % n)
    print("比对 %d 项：一致 %d，关键 3 项已核对" % (checked, matched))
    if advisory:
        print("⚠️ 非关键漂移 %d 处（表可能过期）：%s" % (len(advisory), "; ".join(advisory[:3])))
    if problems:
        print("⛔ 导出表校验失败，**拒绝注入**（防崩闸门）：", file=sys.stderr)
        for p in problems[:6]:
            print("   - " + p, file=sys.stderr)
        print("   引擎很可能已更新。按旧表注入 = 错地址调用 = 游戏必崩（0xc0000005）。", file=sys.stderr)
        print("   先重建：python cli/fxexports.py build <dll>", file=sys.stderr)
        return 9
    print("结论：关键导出全部匹配，可以安全注入 ✓")
    return 0


def cmd_show(args):
    table_path = os.path.abspath(args.table or DEFAULT_TABLE)
    if not os.path.exists(table_path):
        print("导出表文件不存在：%s（先 build 重建）" % table_path, file=sys.stderr)
        return 2
    want = load_table(table_path)
    print("导出表：%s（%d 项）" % (table_path, len(want)))
    for n in CRITICAL:
        if n in want:
            print("  %s\t0x%x" % (n, want[n]))
        else:
            print("  %s\t<缺失>" % n)
    return 0


def main():
    ap = argparse.ArgumentParser(description="重建/核对 neox_engine.dll 导出表")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="从 DLL 重建导出表（旧表备份 .bak）")
    b.add_argument("dll")
    b.add_argument("--out", default="", help="输出表路径（默认 research/tools_extract/neox_exports.txt）")
    v = sub.add_parser("verify", help="核对表与 DLL（关键 3 项失配 = exit 9）")
    v.add_argument("dll")
    v.add_argument("--table", default="", help="表路径（默认同上）")
    s = sub.add_parser("show", help="显示关键 3 项与条目数")
    s.add_argument("--table", default="", help="表路径（默认同上）")
    args = ap.parse_args()
    try:
        if args.cmd == "build":
            if not os.path.isfile(args.dll):
                print("找不到 DLL：%s" % args.dll, file=sys.stderr)
                return 2
            return cmd_build(args)
        if args.cmd == "verify":
            if not os.path.isfile(args.dll):
                print("找不到 DLL：%s" % args.dll, file=sys.stderr)
                return 2
            return cmd_verify(args)
        return cmd_show(args)
    except (ValueError, OSError) as e:
        print("⛔ %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
