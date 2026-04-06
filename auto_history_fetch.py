import os
import re
import sys
import time
import json
import argparse
import subprocess
import codecs
from glob import glob

# ---- prompts / regex ----
CONFIRM_RE = re.compile(r"信息是否正确\?\s*\[y/n\]\s*:\s*", re.M)
CHOICE_RE  = re.compile(r"请输入选项编号\s*:\s*", re.M)

SEM_MENU_RE = re.compile(r"请选择学期", re.M)
RUN_MENU_RE = re.compile(r"请选择一条跑步记录", re.M)

MENU_NUM = re.compile(r"(?m)^\s*\[(\d+)\]\s+")
PAGE_RE  = re.compile(r"第(\d+)\s*/\s*(\d+)\s*页")

SAVED_RE = re.compile(r"记录已成功保存到", re.M)
# 兼容：
#   保存记录到: xxx
#   记录已成功保存到: xxx
SAVE_PATH_RE = re.compile(r"(保存记录到|记录已成功保存到)\s*:\s*(.+)", re.M)


# ----------------- utils -----------------
def parse_page(text: str):
    m = PAGE_RE.search(text or "")
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def menu_indices(text: str):
    return [int(x) for x in MENU_NUM.findall(text or "")]


def max_menu_index(text: str) -> int:
    nums = menu_indices(text)
    return max(nums) if nums else 0


def is_qualified_2(obj) -> bool:
    """递归扫描任意层级 isQualified，兼容 2 / '2'。"""
    if isinstance(obj, dict):
        if "isQualified" in obj and str(obj["isQualified"]) == "2":
            return True
        return any(is_qualified_2(v) for v in obj.values())
    if isinstance(obj, list):
        return any(is_qualified_2(v) for v in obj)
    return False


def to_float(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        s = s.replace(",", ".")
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if m:
            try:
                return float(m.group(0))
            except Exception:
                return None
    return None


def collect_record_mileage_values(obj, out):
    """递归扫描任意层级的 recordMileage。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "recordMileage":
                fv = to_float(v)
                if fv is not None:
                    out.append(fv)
            collect_record_mileage_values(v, out)
    elif isinstance(obj, list):
        for item in obj:
            collect_record_mileage_values(item, out)


def is_record_mileage_ok(obj, threshold: float) -> bool:
    values = []
    collect_record_mileage_values(obj, values)
    return any(threshold < v <= 5 for v in values)


class BufferedWriter:
    """减少 flush 次数（性能关键）"""

    def __init__(self, fp, flush_interval=0.2):
        self.fp = fp
        self.flush_interval = flush_interval
        self.buf = []
        self.last = time.time()

    def write(self, s: str):
        self.buf.append(s)
        now = time.time()
        if (now - self.last) >= self.flush_interval or ("\n" in s):
            self.flush()

    def flush(self):
        if not self.buf:
            return
        self.fp.write("".join(self.buf))
        self.fp.flush()
        self.buf.clear()
        self.last = time.time()


def spawn_py(script_path: str, cwd: str):
    cmd = [sys.executable, "-X", "utf8", "-u", script_path]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=env,
        cwd=cwd,
    )
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return p, decoder


def read_some(p, n: int) -> bytes:
    """
    优先用 read1：交互式 stdout PIPE 下更容易“有多少读多少”，
    不容易卡到等满 n 才返回。
    """
    out = p.stdout
    if hasattr(out, "read1"):
        return out.read1(n)
    return out.read(1)  # fallback（不推荐，但保底）


def read_until(p, decoder, buffer: str, patterns, logw: BufferedWriter, raw_fp=None,
               timeout_s=120, chunk_size=2048):
    """
    从子进程读数据，直到 buffer 命中某个 pattern。
    返回 (hit_index, buffer)：
      hit_index >= 0 : 命中 patterns[hit_index]
      hit_index == -1: EOF
      hit_index == -2: 超时
    """
    start = time.time()
    while True:
        if time.time() - start > timeout_s:
            return -2, buffer

        b = read_some(p, chunk_size)
        if not b:
            return -1, buffer

        if raw_fp:
            raw_fp.write(b)

        s = decoder.decode(b)
        buffer += s
        logw.write(s)

        for i, rgx in enumerate(patterns):
            if rgx.search(buffer):
                return i, buffer


def send_line(p, logw: BufferedWriter, s: str):
    logw.write(f"\n[AUTO->STDIN] {s}\n")
    p.stdin.write((s + "\n").encode("utf-8", errors="replace"))
    p.stdin.flush()


def wait_file_exists(path: str, timeout_s: float = 3.0, interval_s: float = 0.1) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        if os.path.exists(path):
            return True
        time.sleep(interval_s)
    return False


def find_latest_tasklist(script_dir: str, outdir: str, seconds_window: float = 30.0):
    now = time.time()
    cands = []
    for base in [outdir, script_dir]:
        tasks_dir = os.path.join(base, "tasks_else")
        if not os.path.isdir(tasks_dir):
            continue
        for p in glob(os.path.join(tasks_dir, "tasklist_*.json")):
            try:
                mt = os.path.getmtime(p)
            except Exception:
                continue
            if now - mt <= seconds_window:
                cands.append((mt, p))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def delete_if_filtered(outdir: str, script_dir: str, logw: BufferedWriter,
                       captured_output: str, mileage_threshold: float) -> bool:
    """
    你的 json 保存在 tasks_else 下：
    - 优先用程序打印的“保存路径”
    - 同时尝试 outdir + saved_path、script_dir + saved_path
    - 再兜底：找最近生成的 tasks_else/tasklist_*.json

    删除条件：
    - 任意层级 isQualified == 2
    - recordMileage 不存在，或所有 recordMileage 不满足 mileage_threshold < v < 5
    """
    target_paths = []

    m = SAVE_PATH_RE.findall(captured_output or "")
    if m:
        saved_path = m[-1][1].strip().strip('"').strip("'")
        target_paths.append(os.path.normpath(os.path.abspath(os.path.join(outdir, saved_path))))
        target_paths.append(os.path.normpath(os.path.abspath(os.path.join(script_dir, saved_path))))

    fallback = find_latest_tasklist(script_dir, outdir, seconds_window=30.0)
    if fallback:
        target_paths.append(os.path.normpath(os.path.abspath(fallback)))

    uniq = []
    seen = set()
    for pth in target_paths:
        if pth not in seen:
            uniq.append(pth)
            seen.add(pth)

    if not uniq:
        logw.write("\n[WARN] 没抓到保存路径，也没找到 tasklist_*.json，无法过滤。\n")
        return False

    for abs_path in uniq:
        if not wait_file_exists(abs_path, timeout_s=3.0, interval_s=0.1):
            continue
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if is_qualified_2(data):
                os.remove(abs_path)
                logw.write(f"\n[FILTER] isQualified==2 -> deleted: {abs_path}\n")
                return True

            if not is_record_mileage_ok(data, mileage_threshold):
                os.remove(abs_path)
                logw.write(f"\n[KEEP] isQualified!=2 and {mileage_threshold}<recordMileage<=5 -> kept: {abs_path}\n")
                return True

            logw.write(f"\n[KEEP] isQualified!=2 and {mileage_threshold}<recordMileage<5 -> kept: {abs_path}\n")
            return False
        except Exception as e:
            logw.write(f"\n[WARN] 检查/删除失败：{abs_path} err={e}\n")
            continue

    logw.write("\n[WARN] 所有候选路径都不存在或不可读，无法过滤。\n")
    return False


# ----------------- core flows -----------------
def collect_record_indices(script: str, semester: str, outdir: str, chunk_size: int, flush_interval: float):
    script_abs = os.path.abspath(script)
    p, decoder = spawn_py(script_abs, cwd=outdir)

    records = set()
    buf = ""

    os.makedirs(outdir, exist_ok=True)
    session_log = os.path.join(outdir, "session_discover.log")

    with open(session_log, "w", encoding="utf-8", newline="") as slog:
        logw = BufferedWriter(slog, flush_interval=flush_interval)

        # confirm
        hit, buf = read_until(p, decoder, buf, [CONFIRM_RE], logw, timeout_s=120, chunk_size=chunk_size)
        if hit < 0:
            try:
                p.terminate()
            except Exception:
                pass
            return []
        send_line(p, logw, "y")
        buf = ""

        # semester
        hit, buf = read_until(p, decoder, buf, [SEM_MENU_RE, CHOICE_RE], logw, timeout_s=120, chunk_size=chunk_size)
        while not (SEM_MENU_RE.search(buf) and CHOICE_RE.search(buf)):
            hit, buf = read_until(p, decoder, buf, [CHOICE_RE], logw, timeout_s=120, chunk_size=chunk_size)
            if hit < 0:
                try:
                    p.terminate()
                except Exception:
                    pass
                return []
        send_line(p, logw, str(semester))
        buf = ""

        # run list pages
        while True:
            hit, buf = read_until(p, decoder, buf, [RUN_MENU_RE, CHOICE_RE], logw, timeout_s=180, chunk_size=chunk_size)
            while not (RUN_MENU_RE.search(buf) and CHOICE_RE.search(buf)):
                hit, buf = read_until(p, decoder, buf, [CHOICE_RE], logw, timeout_s=180, chunk_size=chunk_size)
                if hit < 0:
                    break

            for x in MENU_NUM.findall(buf):
                records.add(int(x))

            page, total = parse_page(buf)
            logw.write(f"\n[AUTO] discover page={page}/{total}, collected={len(records)}\n")

            if page is not None and total is not None and page < total:
                send_line(p, logw, "n")
                buf = ""
            else:
                send_line(p, logw, "q")
                break

        try:
            p.terminate()
        except Exception:
            pass

    return sorted(records)


def goto_and_select_run_record(p, decoder, logw: BufferedWriter, record_idx: int,
                               chunk_size: int, timeout_s: int = 180):
    """
    关键修复：第3个输入点自动翻页，直到当前页出现 record_idx 再输入。
    """
    buf = ""
    while True:
        hit, buf = read_until(p, decoder, buf, [RUN_MENU_RE, CHOICE_RE], logw, timeout_s=timeout_s, chunk_size=chunk_size)
        while not (RUN_MENU_RE.search(buf) and CHOICE_RE.search(buf)):
            hit, buf = read_until(p, decoder, buf, [CHOICE_RE], logw, timeout_s=timeout_s, chunk_size=chunk_size)
            if hit < 0:
                return False, buf

        nums = menu_indices(buf)
        if not nums:
            # 没解析到编号，别乱输
            logw.write("\n[WARN] 当前页没解析到 [1] 这种编号，停止避免乱输。\n")
            return False, buf

        mn, mx = min(nums), max(nums)
        page, total = parse_page(buf)
        logw.write(f"\n[AUTO] run-menu page={page}/{total}, range=[{mn},{mx}], target={record_idx}\n")

        if record_idx in nums:
            send_line(p, logw, str(record_idx))
            return True, buf

        # 不在当前页 -> 翻页
        if record_idx > mx and page is not None and total is not None and page < total:
            send_line(p, logw, "n")
            buf = ""
            continue
        if record_idx < mn and page is not None and page > 1:
            send_line(p, logw, "p")
            buf = ""
            continue

        # 没法再翻，退出
        logw.write("\n[WARN] 目标编号不在任何可达页面（或页信息缺失），停止。\n")
        return False, buf


def fetch_one_record(script: str, semester: str, record_idx: int, outdir: str, run_id: int,
                     enable_filter: bool, chunk_size: int, flush_interval: float,
                     mileage_threshold: float):
    script_abs = os.path.abspath(script)
    script_dir = os.path.dirname(script_abs)

    p, decoder = spawn_py(script_abs, cwd=outdir)

    buf = ""
    os.makedirs(outdir, exist_ok=True)
    session_log = os.path.join(outdir, f"session_{run_id:03d}_rec{record_idx}.log")

    with open(session_log, "w", encoding="utf-8", newline="") as slog:
        logw = BufferedWriter(slog, flush_interval=flush_interval)

        # confirm
        hit, buf = read_until(p, decoder, buf, [CONFIRM_RE], logw, timeout_s=120, chunk_size=chunk_size)
        if hit < 0:
            try:
                p.terminate()
            except Exception:
                pass
            return "FAIL"
        send_line(p, logw, "y")
        buf = ""

        # semester menu
        hit, buf = read_until(p, decoder, buf, [SEM_MENU_RE, CHOICE_RE], logw, timeout_s=120, chunk_size=chunk_size)
        while not (SEM_MENU_RE.search(buf) and CHOICE_RE.search(buf)):
            hit, buf = read_until(p, decoder, buf, [CHOICE_RE], logw, timeout_s=120, chunk_size=chunk_size)
            if hit < 0:
                try:
                    p.terminate()
                except Exception:
                    pass
                return "FAIL"
        send_line(p, logw, str(semester))
        buf = ""

        # run menu (auto page)
        ok, last_menu_buf = goto_and_select_run_record(
            p, decoder, logw, record_idx, chunk_size=chunk_size, timeout_s=180
        )
        if not ok:
            try:
                p.terminate()
            except Exception:
                pass
            return "FAIL"

        # wait saved
        hit, buf = read_until(p, decoder, "", [SAVED_RE], logw, timeout_s=240, chunk_size=chunk_size)
        ok_saved = (hit == 0)

        status = "FAIL"
        if ok_saved:
            status = "OK"
            if enable_filter:
                # buf 里一般包含保存路径那几行
                if delete_if_filtered(outdir, script_dir, logw, buf, mileage_threshold):
                    status = "FILTERED"

        try:
            p.terminate()
        except Exception:
            pass

        return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script", nargs="?", default="history.py")
    ap.add_argument("--semester", default="2")
    ap.add_argument("--outdir", default="auto_out")
    ap.add_argument("--delay", type=float, default=0.05)          # 比你之前更小
    ap.add_argument("--no-filter", action="store_true")
    ap.add_argument("--chunk", type=int, default=2048)            # 一次读多少字节
    ap.add_argument("--flush", type=float, default=0.2)           # 日志 flush 间隔(秒)
    ap.add_argument("--task", choices=["0", "1"], default=None)
    args = ap.parse_args()

    task = args.task
    while task not in ("0", "1"):
        task = input("请选择任务(0/1): ").strip()

    mileage_threshold = 2.02 if task == "0" else 2.51
    print(f"[TASK] 任务={task}, recordMileage 范围=({mileage_threshold}, 5] (需落在该范围内才保留)")

    os.makedirs(args.outdir, exist_ok=True)

    records = collect_record_indices(
        args.script, args.semester, args.outdir,
        chunk_size=args.chunk, flush_interval=args.flush
    )
    with open(os.path.join(args.outdir, "records.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(map(str, records)) + "\n")

    enable_filter = (not args.no_filter)

    ok_cnt = filtered_cnt = fail_cnt = 0
    for i, rec in enumerate(records, 1):
        status = fetch_one_record(
            args.script, args.semester, rec, args.outdir,
            run_id=i, enable_filter=enable_filter,
            chunk_size=args.chunk, flush_interval=args.flush,
            mileage_threshold=mileage_threshold
        )
        if status == "OK":
            ok_cnt += 1
        elif status == "FILTERED":
            filtered_cnt += 1
        else:
            fail_cnt += 1

        print(f"[FETCH] {i}/{len(records)} rec {rec}: {status}")
        if args.delay > 0:
            time.sleep(args.delay)

    print(f"[DONE] OK={ok_cnt}, FILTERED(deleted)={filtered_cnt}, FAIL={fail_cnt}")
    print(f"[DONE] 输出目录：{args.outdir}")


if __name__ == "__main__":
    main()
