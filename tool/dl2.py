#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Range 分段下载器（可续传 / 多镜像轮换 / 429 退避）。"""
import os
import random
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def head_size(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        cr = r.headers.get("Content-Range")
        if cr and "/" in cr:
            return int(cr.split("/")[-1])
        cl = r.headers.get("Content-Length")
        return int(cl) if cl else 0


def seg_bounds(total, n):
    seg = total // n
    out = []
    for i in range(n):
        s = i * seg
        e = total - 1 if i == n - 1 else (i + 1) * seg - 1
        out.append((i, s, e))
    return out


def fetch_seg(mirrors, start, end, path, seg_i, retries=25):
    want = end - start + 1
    tmp = str(path) + ".tmp"
    for attempt in range(retries):
        url = mirrors[attempt % len(mirrors)]
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Range": f"bytes={start}-{end}",
                "Accept": "*/*", "Accept-Encoding": "identity",
                "Referer": "https://github.com/",
            })
            with urllib.request.urlopen(req, timeout=90) as r, open(tmp, "wb") as f:
                while True:
                    c = r.read(131072)
                    if not c:
                        break
                    f.write(c)
            got = os.path.getsize(tmp)
            if got == want:
                os.replace(tmp, path)
                return seg_i, "ok"
            os.unlink(tmp)
            raise IOError(f"short {got}/{want}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 403, 502, 503, 504):
                ra = e.headers.get("Retry-After")
                sleep = float(ra) if ra and ra.isdigit() else min(2 * (attempt + 1) + random.random() * 3, 30)
                time.sleep(sleep)
            else:
                time.sleep(1.5 * (attempt + 1))
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return seg_i, "FAIL"


def main():
    base = sys.argv[1]
    out = Path(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 24
    workers = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    mirrors = [
        "https://ghfast.top/" + base,
        "https://gh-proxy.com/" + base,
        base,
        "https://ghproxy.net/" + base,
    ]
    total = head_size(mirrors[0])
    print(f"[*] total={total/1048576:.2f}MB n={n} workers={workers}", flush=True)
    parts = out.parent / (out.name + ".parts")
    parts.mkdir(parents=True, exist_ok=True)

    todo = []
    done_bytes = 0
    for i, s, e in seg_bounds(total, n):
        p = parts / f"{i:03d}"
        want = e - s + 1
        if p.exists() and p.stat().st_size == want:
            done_bytes += want
            continue
        if p.exists():
            p.unlink()
        todo.append((i, s, e, p))
    print(f"[*] 已完成 {done_bytes/1048576:.1f}MB，待下 {len(todo)} 段", flush=True)
    if todo:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_seg, mirrors, s, e, p, i): i for i, s, e, p in todo}
            ok = 0
            for f in as_completed(futs):
                i, st = f.result()
                if st == "ok":
                    ok += 1
                el = time.time() - t0
                print(f"    [{ok}/{len(todo)}] seg{i} {st} ({el:.0f}s)", flush=True)
            if ok < len(todo):
                print(f"[!] {len(todo)-ok} 段仍失败，重跑本命令续传即可", flush=True)

    bounds = seg_bounds(total, n)
    missing = [i for i, s, e in bounds
               if not (parts / f"{i:03d}").exists() or (parts / f"{i:03d}").stat().st_size != e - s + 1]
    if missing:
        print(f"[!] 缺段 {missing}", flush=True)
        sys.exit(1)
    with open(out, "wb") as w:
        for i, s, e in bounds:
            w.write((parts / f"{i:03d}").read_bytes())
    for i, s, e in bounds:
        (parts / f"{i:03d}").unlink()
    parts.rmdir()
    print(f"[✓] 合并完成 {out} {out.stat().st_size/1048576:.1f}MB", flush=True)


if __name__ == "__main__":
    main()
