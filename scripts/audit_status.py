"""审计巡检 / 认领 / 跟踪（只读部分不写任何文件；认领与跟踪只写 TEMP 下的锁与日志）。

子命令：
  （默认）         看某系列状态
  --queue          全局下一卷（不认领，纯查询）
  --claim --worker A   按队列认领一个**没被别的 worker 占用**的卷（按卷加锁，可多 worker 并行）
  --release <卷名>      释放该卷的认领
  --finish <卷名>       记一条完成日志并释放
  --gpu-claim / --gpu-release  整卷重排要独占 GPU，重排前后配一对
  --track          吞吐与 ETA（读运行日志）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import tempfile
import time
from pathlib import Path

BASE = Path(os.environ.get("COMICGEN_DATA_ROOT", "."))
TEMP = Path(tempfile.gettempdir()) / "opencode"
LOCK_DIR = TEMP / "locks"
RUN_LOG = TEMP / "audit_runs.jsonl"
GPU_LOCK = TEMP / "gpu.lock"
# 两种锁的窗口都必须长于「合法波次静默期」（一个核对波次 4–5 批 × 15 页，实测 50 分钟左右）：
# 窗口短于静默期，活轮就会被判过期、同一卷被第二个 worker 认领（24 卷撞车的同一类）。
SLOT_STALE_MIN = 60  # 观察员 11-07 19:25 由 40 上调（+50%）：实测合法批次静默期 48–60 分钟，
# 40 分钟窗口在一轮没跑完时先过期，导致同一 worker 提前认领第二卷、同时押两把卷锁。
VOL_STALE_MIN = 60  # 观察员 11-08 07:11 由 50 上调（+20%）：证据 = 死神第05卷 worker A
# 06:00:08 claim 后整波（06:02–06:52 连续产文件）只在 06:52:23 touch 一次，而 06:50:08 就已过
# 50 分钟线 → 06:50:08–06:52:23 这 2 分 15 秒里活锁可被 --claim 当过期锁删掉重新认领（当时
# B 正忙第04卷才没撞上）。卷锁与槽同为 60 后，任一侧都不会比合法静默期短。回退：见
# AUDIT_DECISIONS.md 观察员 07:11 条（下轮若活锁任意一小时内仍需 touch ≥2 次才不被判死、
# 或死锁把队首冻住 >60 分钟，说明方向错了，立即回 50）。
GPU_STALE_MIN = 30
# 与外部程序（comicgen-ui 等）共存：本项目的重排（LaMa）至少要这么多空闲显存才准开工。
# comicgen 在跑时常常只剩几百 MB，硬上会 OOM／把别人的任务拖死。
GPU_MIN_FREE_MB = 2400

SERIES_ORDER = [
    "七龍珠 全彩版",
    "死神 (全74卷)",
    "灌篮高手 (24卷)",
    "海贼王 (繁体)",
]
DONE_MARKERS = ("逐页核对完成", "逐页复核完成")
# 只认这三个明确的"还没交"标记；不要匹配「中断」「未完成」这类会出现在正文叙述里的词
# （11-10 实测：38 卷状态里提到「首轮跑到 160/198 中断」，被误判成未完成重新入队）
PENDING_MARKERS = ("待重排", "待核对", "待补")


def is_done(status: str) -> bool:
    if any(p in status for p in PENDING_MARKERS):
        return False
    return any(m in status for m in DONE_MARKERS)


def needs_render(series_dir: Path, vol: str) -> bool:
    """fix/extra 比 cbz 新（或根本没有 cbz）＝这卷还等着重排，需要 GPU。"""
    cbzs = [p for p in series_dir.glob("*.cbz") if p.name.startswith(vol + " ") or p.name.startswith(vol + " [")]
    cbz = max((p.stat().st_mtime for p in cbzs), default=0.0)
    inputs = [series_dir / f"{vol}.{e}" for e in ("fix.json", "extra.json")]
    newest = max((p.stat().st_mtime for p in inputs if p.exists()), default=0.0)
    if newest == 0.0:
        return False  # 连 fix 都没有＝还没核对，不占 GPU
    return cbz < newest


def progress_rows(progress: Path, series: str) -> list[tuple[str, str, str]]:
    """读出 PROGRESS 的卷行，只留该系列。

    七龍珠 的行写成 `| 七龍珠 全彩版 第27卷 |`（带系列名），但 死神 / 灌篮高手 /
    海贼王 的行只写 `| 死神 第01卷 |`（不带系列名）。旧实现只认「行里含完整系列名」，
    于是这三个系列整列都解析不到 = 永远「未登记」：`--claim` 会把已经做完的卷反复发出去
    （11-08 04:35 实测：第01卷 04:33 刚 finish，2 分钟后又认领同一卷），
    `--track`/`--all` 也一律显示 0 完成。所以同时接受「行含系列名」与
    「卷名以系列首词开头」两种写法。
    """
    if not progress.exists():
        return []
    head = series.split(" ")[0]
    rows = []
    for line in progress.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("卷名", "---"):
            continue
        if series not in line and not cells[0].startswith(head):
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def queue_all(base: Path) -> list[tuple[str, str, str]]:
    """按系列优先级列出全部未完成卷：(系列, 卷名, 状态)。

    可用 `%TEMP%/opencode/scope.txt` 限定只做哪些系列（每行一个系列目录名，空行/不存在＝不限）。
    """
    scope_file = TEMP / "scope.txt"
    scope: set[str] | None = None
    if scope_file.exists():
        lines = [l.strip() for l in scope_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        if lines:
            scope = set(lines)

    out: list[tuple[str, str, str]] = []
    for series in SERIES_ORDER:
        if scope is not None and series not in scope:
            continue
        series_dir = base / series
        if not series_dir.is_dir():
            continue
        suffix = " [300dpi 简].cbz"
        vols = sorted(p.name[: -len(suffix)] for p in series_dir.glob("*.cbz") if p.name.endswith(suffix))
        status_of = {v: st for v, st, _ in progress_rows(base / "PROGRESS.md", series)}
        out.extend((series, v, status_of.get(v, "未登记")) for v in vols if not is_done(status_of.get(v, "")))
    return out


def _key(series: str, vol: str) -> str:
    return (series + "__" + vol).replace(" ", "").replace("(", "（").replace(")", "）").replace("/", "_")


def _lock_path(series: str, vol: str) -> Path:
    return LOCK_DIR / (_key(series, vol) + ".lock")


def log_event(worker: str, event: str, series: str = "", vol: str = "", note: str = "") -> None:
    TEMP.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "worker": worker,
        "event": event,
        "series": series,
        "vol": vol,
        "note": note,
    }
    with RUN_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _slot_path(worker: str) -> Path:
    return LOCK_DIR / f"slot_{worker}.lock"


def claim(base: Path, worker: str, stale_min: int = SLOT_STALE_MIN) -> tuple[str, str, str] | None | str:
    """返回 (系列, 卷, 状态) / None=没有可认领 / "busy..."=本工人已有活（轮或卷）。

    stacking 根治：不管 slot 是否过期，只要本工人名下还有新鲜卷锁就拒绝新认领。
    提示词自觉靠不住（19:41/20:00 两次叠认领都是提示词已写自检之后发生的）故放代码里硬拦。
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    slot = _slot_path(worker)
    if slot.exists():
        if now - slot.stat().st_mtime < stale_min * 60:
            return "busy-slot"
        slot.unlink(missing_ok=True)
    # 同一工人名下的新鲜卷锁 = 它还有没收尾的卷不许再拿（slot 过期不代表卷做完了）
    for f in LOCK_DIR.glob("*.lock"):
        if f.name.startswith("slot_"):
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if worker in content and now - f.stat().st_mtime < VOL_STALE_MIN * 60:
            return f"busy-vol:{f.stem}"
    # 显存现在够不够：不够就只认领「不用重排」的卷（纯核对），把等着渲染的卷留给 GPU 空闲时。
    free = gpu_free_mb()
    gpu_ok = free is None or free >= GPU_MIN_FREE_MB
    skipped_render = 0
    for series, vol, status in queue_all(base):
        path = _lock_path(series, vol)
        if path.exists():
            if now - path.stat().st_mtime < VOL_STALE_MIN * 60:
                continue  # 别的 worker 正在做
            path.unlink(missing_ok=True)  # 过期锁当没有
        if not gpu_ok and needs_render(base / series, vol):
            skipped_render += 1
            continue  # 显存不够，先跳过等着重排的卷，去拿还能纯核对的
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} | {worker} | {series} | {vol}\n")
        slot.write_text(f"{dt.datetime.now().isoformat(timespec='seconds')} | {worker} | free={free}MB\n", encoding="utf-8")
        log_event(worker, "claim", series, vol, note=f"gpu_free={free}MB")
        return series, vol, status
    if skipped_render:
        print(f"（显存只剩 {free} MB，跳过 {skipped_render} 个待重排的卷，只找纯核对的）")
    return None


def find_series_of(base: Path, vol: str) -> str:
    for series in SERIES_ORDER:
        if (base / series / f"{vol} [300dpi 简].cbz").exists():
            return series
    return ""


def release(base: Path, vol: str) -> bool:
    series = find_series_of(base, vol)
    path = _lock_path(series, vol)
    if path.exists():
        path.unlink()
        log_event("", "release", series, vol)
        return True
    return False


def touch(base: Path, vol: str, worker: str) -> bool:
    """续锁：卷锁 + 工人槽同时刷新时间戳。内容里必须是自己才许续防别人替你续。"""
    series = find_series_of(base, vol)
    if not series:
        print(f"找不到卷：{vol}")
        return False
    path = _lock_path(series, vol)
    if not path.exists():
        print(f"该卷没有锁（可能已过期被别人认领）：{vol}")
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        content = ""
    if worker not in content:
        print(f"锁在别人手里不许续：{vol}")
        return False
    now = dt.datetime.now().isoformat(timespec="seconds")
    path.write_text(f"{now} | {worker} | {series} | {vol} | touch\n", encoding="utf-8")
    _slot_path(worker).write_text(f"{now} | {worker} | touch {vol}\n", encoding="utf-8")
    return True


def gpu_free_mb() -> int | None:
    """当前空闲显存（MB）。取不到（无 nvidia-smi／解析失败）返回 None＝放行。"""
    import subprocess

    smi = r"C:\Windows\System32\nvidia-smi.exe"
    if not Path(smi).exists():
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


def gpu_claim(worker: str, stale_min: int = GPU_STALE_MIN, min_free_mb: int = GPU_MIN_FREE_MB) -> bool:
    TEMP.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if GPU_LOCK.exists():
        if now - GPU_LOCK.stat().st_mtime < stale_min * 60:
            owner = ""
            try:
                owner = GPU_LOCK.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            print(f"GPU 锁已被占：{owner}")
            return False
        GPU_LOCK.unlink(missing_ok=True)
    free = gpu_free_mb()
    if free is not None and free < min_free_mb:
        print(f"显存不足：空闲 {free} MB < 需要 {min_free_mb} MB（多半是 comicgen 等外部程序在跑），本次不拿 GPU")
        return False
    try:
        fd = os.open(GPU_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} | {worker} | free={free}MB\n")
    print(f"GPU 已占用（空闲 {free} MB）")
    return True


def gpu_release() -> None:
    GPU_LOCK.unlink(missing_ok=True)


def track(base: Path) -> int:
    pending = queue_all(base)
    if RUN_LOG.exists():
        recs = [json.loads(l) for l in RUN_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    else:
        recs = []
    finished = sorted((r for r in recs if r["event"] == "finish"), key=lambda r: r["ts"])
    now = dt.datetime.now()
    recent = [r for r in finished if (now - dt.datetime.fromisoformat(r["ts"])).total_seconds() <= 12 * 3600]
    per_vol = None
    if len(recent) >= 2:
        t0 = dt.datetime.fromisoformat(recent[0]["ts"])
        t1 = dt.datetime.fromisoformat(recent[-1]["ts"])
        span = (t1 - t0).total_seconds() / 60
        if span > 0:
            per_vol = span / (len(recent) - 1)

    print(f"=== 跟踪 {now.strftime('%Y-%m-%d %H:%M')} ===")
    print(f"剩余未完成卷：{len(pending)}")
    if pending:
        s, v, _ = pending[0]
        print(f"队首：{v}（{s}）")
    print(f"日志累计完成：{len(finished)} 卷；近 12 小时：{len(recent)} 卷")
    if per_vol:
        print(f"近期节奏：约 {per_vol:.0f} 分钟/卷")
        eta_h = per_vol * len(pending) / 60
        print(f"按此速度剩 {eta_h:.1f} 小时（约 {eta_h / 24:.1f} 天）")
    else:
        print("节奏样本不足（至少要 2 条 finish 记录）")
    if pending:
        print("\n接下来 5 卷：")
        for s, v, st in pending[:5]:
            print(f"  - {v}（{s}） {st[:40]}")
    return 0


def write_tracking_md(base: Path) -> Path:
    """把跟踪快照写成给人看的 markdown（覆盖写）。"""
    pending = queue_all(base)
    recs = []
    if RUN_LOG.exists():
        recs = [json.loads(l) for l in RUN_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    finished = sorted((r for r in recs if r["event"] == "finish"), key=lambda r: r["ts"])
    now = dt.datetime.now()

    lines = ["# 审计进度跟踪", "", f"生成时间：{now.strftime('%Y-%m-%d %H:%M')}", ""]
    lines += ["| 系列 | 已完成 | 总数 | 进度 |", "|---|---|---|---|"]
    grand_total = grand_done = 0
    for series in SERIES_ORDER:
        sdir = base / series
        if not sdir.is_dir():
            continue
        suffix = " [300dpi 简].cbz"
        vols = [p.name[: -len(suffix)] for p in sdir.glob("*.cbz") if p.name.endswith(suffix)]
        status_of = {v: st for v, st, _ in progress_rows(base / "PROGRESS.md", series)}
        done = sum(1 for v in vols if is_done(status_of.get(v, "")))
        grand_total += len(vols)
        grand_done += done
        bar = "█" * int(20 * done / max(len(vols), 1)) + "░" * (20 - int(20 * done / max(len(vols), 1)))
        lines.append(f"| {series} | {done} | {len(vols)} | `{bar}` |")
    lines.append(f"| **合计** | **{grand_done}** | **{grand_total}** | |")

    per_vol = None
    recent = [r for r in finished if (now - dt.datetime.fromisoformat(r["ts"])).total_seconds() <= 12 * 3600]
    if len(recent) >= 2:
        span = (dt.datetime.fromisoformat(recent[-1]["ts"]) - dt.datetime.fromisoformat(recent[0]["ts"])).total_seconds() / 60
        if span > 0:
            per_vol = span / (len(recent) - 1)

    lines += ["", f"- 剩余未完成：**{len(pending)} 卷**"]
    if per_vol:
        lines.append(f"- 近期节奏：约 **{per_vol:.0f} 分钟/卷**")
        lines.append(f"- 按此速度：约 **{per_vol * len(pending) / 60:.1f} 小时**（{per_vol * len(pending) / 1440:.1f} 天）")
    else:
        lines.append("- 节奏样本不足（需 ≥2 条完成记录）")

    if finished:
        lines += ["", "## 最近完成", "", "| 时间 | 卷 |", "|---|---|"]
        for r in finished[-10:][::-1]:
            lines.append(f"| {r['ts'][5:16].replace('T', ' ')} | {r['vol']} |")

    if pending:
        lines += ["", "## 接下来", ""]
        for s, v, st in pending[:8]:
            lines.append(f"- {v}（{s}）")

    out = base / "AUDIT_TRACKING.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="审计巡检 / 认领 / 跟踪")
    ap.add_argument("--series", default="七龍珠 全彩版", help="系列目录名，默认 七龍珠 全彩版")
    ap.add_argument("--base", default=str(BASE), help="_300dpi_简 根目录")
    ap.add_argument("--all", action="store_true", help="列出该系列全部卷")
    ap.add_argument("--queue", action="store_true", help="全局下一卷（不认领）")
    ap.add_argument("--claim", action="store_true", help="认领一个未被占用卷")
    ap.add_argument("--worker", default="A", help="worker 名（认领/日志用）")
    ap.add_argument("--release", metavar="卷名", help="释放认领")
    ap.add_argument("--touch", metavar="卷名", help="续锁（每完成一个阶段跑一次）")
    ap.add_argument("--finish", metavar="卷名", help="记完成并释放")
    ap.add_argument("--gpu-claim", action="store_true", help="独占 GPU（重排前）")
    ap.add_argument("--gpu-release", action="store_true", help="释放 GPU（重排后）")
    ap.add_argument("--track", action="store_true", help="吞吐与 ETA")
    ap.add_argument("--md", action="store_true", help="同时刷新 AUDIT_TRACKING.md")
    args = ap.parse_args(argv)

    base = Path(args.base)

    if args.track:
        rc = track(base)
        if args.md:
            p = write_tracking_md(base)
            print(f"\n已刷新 {p}")
        return rc

    if args.gpu_claim:
        ok = gpu_claim(args.worker)
        if not ok:
            print("<<<GPU 不可用（显存不足或被占），本轮别重排>>>")
        return 0 if ok else 1

    if args.gpu_release:
        gpu_release()
        print("GPU 已释放")
        return 0

    if args.touch:
        print("已续锁" if touch(base, args.touch, args.worker) else "续锁失败")
        return 0

    if args.release:
        ok = release(base, args.release)
        _slot_path(args.worker).unlink(missing_ok=True)
        print("已释放" if ok else "没有该卷的锁（slot 已清）")
        return 0

    if args.finish:
        series = find_series_of(base, args.finish)
        log_event(args.worker, "finish", series, args.finish)
        release(base, args.finish)
        _slot_path(args.worker).unlink(missing_ok=True)
        print(f"已记录完成：{args.finish}")
        return 0

    if args.claim:
        got = claim(base, args.worker)
        if isinstance(got, str) and got.startswith("busy-vol:"):
            print(f"<<<worker {args.worker} 手里还有没收尾的卷 {got[9:]}先做完它（或 --release 并写接续点）>>>")
            return 0
        if got == "busy-slot":
            print(f"<<<worker {args.worker} 已有一轮在跑，跳过>>>")
            return 0
        if got is None:
            print("<<<没有可认领的卷>>>")
            return 0
        series, vol, status = got
        print(f"认领：{vol}")
        print(f"系列目录：{base / series}")
        print(f"当前状态：{status}")
        return 0

    if args.queue:
        pending = queue_all(base)
        if not pending:
            print("<<<全部完成>>>")
            return 0
        series, vol, status = pending[0]
        print(f"下一卷：{vol}")
        print(f"系列目录：{base / series}")
        print(f"当前状态：{status}")
        print(f"总剩余：{len(pending)} 卷")
        return 0

    return show_series(base, args.series, args.all)


def fmt_time(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def fmt_age(ts: float, now: float) -> str:
    mins = int((now - ts) / 60)
    if mins < 60:
        return f"{mins} 分钟前"
    hours = mins // 60
    if hours < 24:
        return f"{hours} 小时前"
    return f"{hours // 24} 天前"


def show_series(base: Path, series: str, show_all: bool) -> int:
    series_dir = base / series
    now = time.time()
    print(f"=== 巡检 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    if GPU_LOCK.exists():
        print(f"GPU：占用中（{fmt_age(GPU_LOCK.stat().st_mtime, now)}）")
    else:
        print("GPU：空闲")
    print(f"系列：{series_dir}")
    print()

    rows = progress_rows(base / "PROGRESS.md", series)
    if not rows:
        print("PROGRESS.md 里没找到该系列的行")
        return 0

    todo = [r for r in rows if not is_done(r[1])]
    done = [r for r in rows if is_done(r[1])]
    show = rows if show_all else todo + done[-3:]

    print(f"进度：共 {len(rows)} 卷，已完成 {len(done)}，未完成 {len(todo)}")
    if todo:
        print(f"下一卷：{todo[0][0]}（{todo[0][1]}）")
    print()
    for vol, status, pages in show:
        mark = "✔" if is_done(status) else "…"
        art = artifacts(series_dir, vol)
        print(f"{mark} {vol}  [{pages}]")
        print(f"    状态：{status}")
        print(f"    产物：audit {art['audit']} | fix {art['fix']} | review {art['review']} | cbz {art['cbz']}")
    return 0


def artifacts(series_dir: Path, vol: str) -> dict[str, str]:
    out = {}
    for suffix, label in ((".audit.md", "audit"), (".fix.json", "fix"), (".pagedreview.md", "review")):
        p = series_dir / f"{vol}{suffix}"
        out[label] = fmt_time(p.stat().st_mtime) if p.exists() else "缺"
    cbzs = sorted(series_dir.glob(f"{vol} *.cbz"))
    out["cbz"] = f"{fmt_time(cbzs[-1].stat().st_mtime)} / {cbzs[-1].stat().st_size // 1048576} MB" if cbzs else "缺"
    return out


if __name__ == "__main__":
    raise SystemExit(main())
