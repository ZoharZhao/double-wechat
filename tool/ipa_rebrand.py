#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ipa_rebrand.py — 重打包 IPA：批量改 CFBundleIdentifier / 显示名，剥离签名，产出可直接
丢给 Sideloadly 自行重签的 IPA。

用法:
  python3 ipa_rebrand.py -i in.ipa -o out.ipa \
      --bundle-id com.tencent.xin1 \
      --display-name 微信1 \
      --name weixin1

行为:
  1. 解包 Payload/*.app
  2. 主 bundle 的 CFBundleIdentifier 换成 --bundle-id；嵌套 .appex/.app/Watch 的
     bundle id 按旧主 id 前缀等比替换（iOS 要求扩展 id 必须以主 id 为前缀）
  3. 主 bundle 的 CFBundleDisplayName / CFBundleName 改成 --display-name / --name
  4. 所有 *.lproj/InfoPlist.strings 里的 CFBundleDisplayName / CFBundleName 一起改
  5. 删 _CodeSignature / SC_Info / embedded.mobileprovision，并对 Mach-O 移除签名
  6. 以 Payload/ 为根重新 zip（不带 __MACOSX、不带 .DS_Store）
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

MACHO_MAGICS = {
    b"\xca\xfe\xba\xbe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbf",
}


def log(msg):
    print(msg, flush=True)


def is_macho(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(4) in MACHO_MAGICS
    except OSError:
        return False


def read_plist(p: Path):
    data = p.read_bytes()
    fmt = plistlib.FMT_BINARY if data[:8] == b"bplist00" else plistlib.FMT_XML
    return plistlib.loads(data), fmt


def write_plist(p: Path, obj, fmt):
    p.write_bytes(plistlib.dumps(obj, fmt=fmt))


def find_bundles(app: Path):
    """主 app 及其内部所有 .app / .appex 包。"""
    bundles = []
    if (app / "Info.plist").is_file():
        bundles.append(app)
    for d in sorted(app.rglob("*")):
        if d.is_dir() and d.suffix.lower() in (".app", ".appex") and (d / "Info.plist").is_file():
            bundles.append(d)
    return bundles


def drop_broken_bundles(app: Path):
    """
    砸壳包常见的残骸：嵌套 .app/.appex 的 Info.plist 声明了 CFBundleExecutable，
    但可执行文件被砸壳者删掉了（典型 com.apple.WatchPlaceholder）。
    installd / libimobiledevice 会因此报 bundle_main_missing (code -77)，
    这种 bundle 只能整体删掉。
    """
    dropped = []
    for b in sorted(app.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not b.is_dir() or b.suffix.lower() not in (".app", ".appex"):
            continue
        ip = b / "Info.plist"
        if not ip.is_file():
            continue
        try:
            info, _ = read_plist(ip)
        except Exception:
            continue
        ex = info.get("CFBundleExecutable")
        if not ex:
            continue
        if not (b / ex).is_file():
            dropped.append((b, ex))
    for b, ex in dropped:
        log(f"    ✗ 残缺 bundle（缺可执行 {ex}）→ 删除: {b.relative_to(app)}")
        shutil.rmtree(b, ignore_errors=True)
    # 清掉删空后的 WatchPlaceholder 之类的壳目录
    for d in sorted(app.rglob("com.apple.WatchPlaceholder"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            log(f"    ✗ 删除空壳目录: {d.relative_to(app)}")
    return dropped


def fix_device_restrictions(info: dict, drop: bool, add_models: list):
    """
    UISupportedDevices 是 App Store 侧的分发白名单。从 Mac App Store / iPad 渠道
    抓出来的包，这一项只有 iPad* 和 MacFamily*，装到 iPhone 上 installd 会报
    DeviceNotSupportedByThinning。两种解法：整个删掉，或把本机型号加进数组。
    """
    changed = []
    models = info.get("UISupportedDevices")
    if isinstance(models, list) and models:
        new = list(models)
        for m in add_models:
            if m and m not in new:
                new.append(m)
                changed.append(f"加入 {m}")
        if drop:
            info.pop("UISupportedDevices", None)
            changed.append(f"删除 UISupportedDevices（原 {len(models)} 项）")
        else:
            info["UISupportedDevices"] = new
    fam = info.get("UIDeviceFamily")
    if isinstance(fam, list) and 1 not in fam:
        info["UIDeviceFamily"] = sorted(set(fam) | {1})
        changed.append("UIDeviceFamily 补上 1 (iPhone)")
    return changed


def map_bundle_id(old_id: str, old_root: str, new_root: str):
    if old_id == old_root:
        return new_root
    if old_id.startswith(old_root + "."):
        return new_root + old_id[len(old_root):]
    return None


def strip_signatures(root: Path):
    """删除签名相关文件，并对 Mach-O 执行 codesign --remove-signature。"""
    removed = 0
    for d in list(root.rglob("_CodeSignature")):
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    for name in ("SC_Info",):
        for d in list(root.rglob(name)):
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    for f in list(root.rglob("embedded.mobileprovision")):
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass

    unsigned = 0
    for f in root.rglob("*"):
        if not f.is_file() or f.is_symlink():
            continue
        if not is_macho(f):
            continue
        try:
            r = subprocess.run(["codesign", "--remove-signature", str(f)],
                               capture_output=True, text=True)
            if r.returncode == 0:
                unsigned += 1
        except FileNotFoundError:
            pass
    return removed, unsigned


def fix_permissions(root: Path):
    """Mach-O 可执行权限必须 0755，否则 installd 拒绝执行。"""
    fixed = 0
    for f in root.rglob("*"):
        if not f.is_file() or f.is_symlink():
            continue
        try:
            mode = f.stat().st_mode & 0o777
            if is_macho(f):
                if mode != 0o755:
                    f.chmod(0o755)
                    fixed += 1
            elif mode & 0o111:
                f.chmod(0o644)
                fixed += 1
        except OSError:
            pass
    return fixed


def patch_strings(strings_file: Path, new_display: str, new_name: str):
    """改本地化 InfoPlist.strings 里的显示名。"""
    try:
        data = strings_file.read_bytes()
    except OSError:
        return False
    if not data:
        return False
    fmt = plistlib.FMT_BINARY if data[:8] == b"bplist00" else plistlib.FMT_XML
    try:
        obj = plistlib.loads(data)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    changed = False
    if "CFBundleDisplayName" in obj and new_display:
        obj["CFBundleDisplayName"] = new_display
        changed = True
    if "CFBundleName" in obj and new_name:
        obj["CFBundleName"] = new_name
        changed = True
    if changed:
        strings_file.write_bytes(plistlib.dumps(obj, fmt=fmt))
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", required=True)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--bundle-id", required=True, help="新的主 bundle id，如 com.tencent.xin1")
    ap.add_argument("--display-name", default="", help="新的显示名，如 微信1")
    ap.add_argument("--name", default="", help="新的 CFBundleName，如 weixin1")
    ap.add_argument("--keep-signature", action="store_true", help="保留原签名（默认剥离）")
    ap.add_argument("--keep-broken-bundles", action="store_true",
                    help="保留缺少可执行文件的嵌套 bundle（默认自动删除，否则安装报 code -77）")
    ap.add_argument("--keep-supported-devices", action="store_true",
                    help="保留 UISupportedDevices 白名单（默认删除，否则 iPhone 装 iPad/Mac 包报 DeviceNotSupportedByThinning）")
    ap.add_argument("--version", default="", help="覆写 CFBundleShortVersionString（伪装版本绕过强制升级提示）")
    ap.add_argument("--build", default="", help="覆写 CFBundleVersion / WeChatBundleVersion")
    ap.add_argument("--add-device", action="append", default=[],
                    metavar="MODEL", help="往 UISupportedDevices 追加设备型号，如 iPhone17,3")
    ap.add_argument("--workdir", default="", help="解包目录（默认临时目录）")
    args = ap.parse_args()

    src = Path(args.input).resolve()
    dst = Path(args.output).resolve()
    if not src.is_file():
        log(f"[!] 找不到输入文件: {src}")
        sys.exit(1)
    if dst.exists():
        log(f"[!] 输出已存在，先删除: {dst}")
        dst.unlink()

    work = Path(args.workdir).resolve() if args.workdir else Path(tempfile.mkdtemp(prefix="iparebrand_"))
    work.mkdir(parents=True, exist_ok=True)
    log(f"[*] 解包 {src.name} -> {work}")
    # 用系统 unzip 解包：保留 Unix 权限位与符号链接（python zipfile 会丢 0755）
    r = subprocess.run(["unzip", "-q", "-o", str(src), "-d", str(work)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"[!] unzip 失败: {r.stderr}")
        sys.exit(2)

    payload = work / "Payload"
    if not payload.is_dir():
        log("[!] IPA 里没有 Payload/ 目录")
        sys.exit(2)
    apps = [d for d in payload.iterdir() if d.is_dir() and d.suffix.lower() == ".app"]
    if not apps:
        log("[!] Payload/ 里没有 .app")
        sys.exit(2)
    app = apps[0]
    log(f"[*] 主 app: {app.name}")

    main_plist_path = app / "Info.plist"
    main_plist, main_fmt = read_plist(main_plist_path)
    old_root = main_plist.get("CFBundleIdentifier", "")
    if not old_root:
        log("[!] 主 Info.plist 缺少 CFBundleIdentifier")
        sys.exit(2)
    new_root = args.bundle_id
    log(f"[*] 主 bundle id: {old_root}  ->  {new_root}")

    if not args.keep_broken_bundles:
        dropped = drop_broken_bundles(app)
        log(f"[*] 清理残缺 bundle: {len(dropped)} 个")

    # 1) 所有嵌套 bundle 的 id
    bundles = find_bundles(app)
    id_map = {}
    for b in bundles:
        ip = b / "Info.plist"
        try:
            info, fmt = read_plist(ip)
        except Exception as e:
            log(f"[!] 读取失败 {ip}: {e}")
            continue
        old_id = info.get("CFBundleIdentifier", "")
        if not old_id:
            continue
        new_id = map_bundle_id(old_id, old_root, new_root)
        if new_id is None:
            new_id = old_id
            log(f"    · 跳过（非主 id 命名空间）{b.relative_to(app)}: {old_id}")
        else:
            info["CFBundleIdentifier"] = new_id
            id_map[old_id] = new_id
            if b == app and args.display_name:
                info["CFBundleDisplayName"] = args.display_name
            if b == app and args.name:
                info["CFBundleName"] = args.name
            write_plist(ip, info, fmt)
            log(f"    · {b.relative_to(app) if b != app else '.'}: {old_id} -> {new_id}")

    # 版本号伪装
    if args.version or args.build:
        mp, mf = read_plist(main_plist_path)
        if args.version:
            mp["CFBundleShortVersionString"] = args.version
            log(f"    · 版本号: CFBundleShortVersionString -> {args.version}")
        if args.build:
            mp["CFBundleVersion"] = args.build
            if "WeChatBundleVersion" in mp:
                mp["WeChatBundleVersion"] = args.build
            log(f"    · 版本号: CFBundleVersion -> {args.build}")
        write_plist(main_plist_path, mp, mf)

    # 设备白名单
    main_plist, main_fmt = read_plist(main_plist_path)
    dev_changes = fix_device_restrictions(
        main_plist, drop=not args.keep_supported_devices, add_models=args.add_device)
    if dev_changes:
        write_plist(main_plist_path, main_plist, main_fmt)
        for c in dev_changes:
            log(f"    · 设备限制: {c}")

    # 主 plist 兜底（有时没有 CFBundleDisplayName 键）
    if args.display_name and "CFBundleDisplayName" not in main_plist:
        main_plist, main_fmt = read_plist(main_plist_path)
        main_plist["CFBundleDisplayName"] = args.display_name
        write_plist(main_plist_path, main_plist, main_fmt)
        log(f"    · 新增 CFBundleDisplayName = {args.display_name}")

    # 2) 本地化 InfoPlist.strings
    for lproj in app.glob("*.lproj"):
        sf = lproj / "InfoPlist.strings"
        if sf.is_file():
            if patch_strings(sf, args.display_name, args.name):
                log(f"    · {lproj.name}/InfoPlist.strings 已更新")

    # 3) 剥离签名
    if not args.keep_signature:
        removed, unsigned = strip_signatures(work)
        log(f"[*] 已删除 {removed} 个签名残留，移除 {unsigned} 个 Mach-O 签名")
    # 打包前自检：每个 bundle 的主可执行必须存在
    problems = []
    for b in [app] + [d for d in app.rglob("*") if d.is_dir() and d.suffix.lower() in (".app", ".appex")]:
        ip = b / "Info.plist"
        if not ip.is_file():
            continue
        try:
            info, _ = read_plist(ip)
        except Exception:
            continue
        ex = info.get("CFBundleExecutable")
        if ex and not (b / ex).is_file():
            problems.append(f"{b.relative_to(app)}/{ex}")
    if problems:
        log("[!] 仍有 bundle 缺可执行：" + ", ".join(problems))
        sys.exit(4)
    log("[*] bundle 完整性自检: 通过")

    perm = fix_permissions(app.parent)
    log(f"[*] 修正 {perm} 个文件权限（Mach-O -> 0755）")

    # 4) 重新打包
    log(f"[*] 打包 -> {dst.name}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["zip", "-qry", str(dst), "Payload",
           "-x", "*.DS_Store", "-x", "__MACOSX/*"]
    r = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True)
    if r.returncode != 0:
        log(f"[!] zip 失败: {r.stderr}")
        sys.exit(3)

    size = dst.stat().st_size / 1048576
    log(f"[✓] 完成: {dst}  ({size:.1f} MB)")
    log("")
    log("[*] 校验清单:")
    with zipfile.ZipFile(dst) as z:
        bad = z.testzip()
        log(f"    zip 完整性: {'OK' if bad is None else '损坏 @ ' + bad}")
        names = z.namelist()
        log(f"    条目数: {len(names)}")
        for n in names:
            if n.count("/") == 2 and n.endswith("Info.plist"):
                log(f"    包含: {n}")
    if not args.workdir:
        log(f"[*] 临时目录保留在 {work}（自行删除）")


if __name__ == "__main__":
    main()
