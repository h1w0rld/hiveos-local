#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive-blackbox: persistent flight recorder for mining rigs.

Why: /var/log and journald live in tmpfs on these rigs, so every reboot erases
the evidence of why the rig froze / lost power / lost its network (RIG9 21 Oct,
RIG1 23+25 Oct, RIG5 26 Oct). This daemon streams EVERYTHING into one folder on
the real disk (ext4), fsync'ed every second, one sub-folder per boot:

    /var/lib/hive-blackbox/boots/0007_20261027-022752/
        blackbox.log     chronological: kernel ring buffer, journald, hive-agent,
                         miner, nvidia-oc, panel, netbird logs (tagged by source)
        telemetry.jsonl  one record every 5 s (load, memory, D-state processes,
                         temps, network, hashrate, watchdog) + GPU/gateway probes
        meta.json        boot id, start time, watchdog bootstatus, clean-stop mark

Everything lives under ONE directory: `hive_blackbox.py purge --all` (or just
rm -rf /var/lib/hive-blackbox) removes all of it.

After a reboot:  hive_blackbox.py report      -> what happened to the previous boot

Stdlib only, python >= 3.8.
"""
import argparse
import collections
import glob
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

VERSION = 1
BASE = os.environ.get("HIVE_BLACKBOX_DIR", "/var/lib/hive-blackbox")
BOOTS = os.path.join(BASE, "boots")

MAX_TOTAL_BYTES = int(os.environ.get("HIVE_BLACKBOX_MAX_BYTES", 3 * 1024 ** 3))
MAX_BOOTS = 120
KEEP_RECENT_BOOTS = 3            # never pruned (current + 2 previous)
LOG_ROTATE_BYTES = 100 * 1024 ** 2
LOG_ROTATE_KEEP = 4
TELE_ROTATE_BYTES = 40 * 1024 ** 2
TELE_ROTATE_KEEP = 3
MIN_FREE_BYTES = 2 * 1024 ** 3   # below this: prune harder
FSYNC_EVERY = 1.0
TELEMETRY_EVERY = 5.0
PROBE_EVERY = 10.0               # nvidia-smi + gateway ping
LINE_MAX = 2000
RATE_LINES_PER_SEC = 300         # per source, then "dropped N lines"

FILE_LOGS = ["/var/log/hive-agent.log", "/var/log/nvidia-oc.log", "/var/log/hiveos-local.log",
             "/var/log/netbird.log", "/var/log/netbird-watchdog.log"]
FILE_LOG_GLOBS = ["/var/log/miner/*/*.log", "/var/log/miner/*.log"]

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

SEVERE_RE = re.compile(
    r"Xid|NVRM: .*(fail|error|fall|lost)|Oops|BUG:|Call Trace|kernel panic|segfault|general protection|"
    r"hung task|blocked for more than|soft lockup|hard LOCKUP|rcu.*stall|Out of memory|oom-kill|"
    r"Killed process|Machine check|\bMCE\b|EDAC|Thermal|thermal|throttl|temperature above|"
    r"usb \S+: .*(disconnect|reset|error|fail)|I/O error|EXT4-fs error|ata\d+.*(error|exception|reset|failed)|"
    r"PCIe Bus Error|AER:|watchdog.*(reset|timeout|expired)|nvidia-smi TIMEOUT|"
    r"wlan\d.*(disconnect|deauth|lost|link is not ready)|rtl8xxxu.*(fail|error|timeout)|Net-heal|"
    r"sreboot|Reached target (Reboot|Power-Off)|systemd-shutdown", re.I)


# ----------------------------------------------------------------- helpers

def _now():
    return time.time()


def _ts(t=None):
    t = _now() if t is None else t
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + (".%03d" % int((t % 1) * 1000))


def _read(path, default=""):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return default


def _uptime():
    try:
        return float(_read("/proc/uptime").split()[0])
    except Exception:
        return 0.0


def _boot_id():
    return _read("/proc/sys/kernel/random/boot_id") or ("noboot-%d" % int(_now() - _uptime()))


def _dir_size(path):
    total = 0
    for root, _d, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def list_boots():
    """Boot directories oldest -> newest."""
    try:
        names = sorted(n for n in os.listdir(BOOTS) if re.match(r"^\d{4,}_", n))
    except OSError:
        return []
    return [os.path.join(BOOTS, n) for n in names]


def read_meta(boot_dir):
    try:
        with open(os.path.join(boot_dir, "meta.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def prune_boots(current_dir=None):
    """Delete the oldest boot folders beyond the size/count caps."""
    boots = list_boots()
    protected = set(boots[-KEEP_RECENT_BOOTS:])
    if current_dir:
        protected.add(current_dir)
    try:
        free = shutil.disk_usage(BASE).free
    except Exception:
        free = MIN_FREE_BYTES * 2
    sizes = {b: _dir_size(b) for b in boots}
    total = sum(sizes.values())
    removed = []
    for b in boots:
        over = total > MAX_TOTAL_BYTES or len(boots) - len(removed) > MAX_BOOTS or free < MIN_FREE_BYTES
        if not over:
            break
        if b in protected:
            continue
        shutil.rmtree(b, ignore_errors=True)
        total -= sizes[b]
        free += sizes[b]
        removed.append(b)
    return removed


# ----------------------------------------------------------------- writer

class Writer:
    """Single writer thread: queues -> files, fsync once per second, rotation."""

    def __init__(self, boot_dir):
        self.boot_dir = boot_dir
        self.q = queue.Queue(maxsize=50000)
        self.paths = {"log": os.path.join(boot_dir, "blackbox.log"),
                      "tele": os.path.join(boot_dir, "telemetry.jsonl")}
        self.files = {k: open(p, "a", buffering=1 << 16, encoding="utf-8", errors="replace")
                      for k, p in self.paths.items()}
        self.stop = threading.Event()
        self.dropped_queue = 0
        self.thread = threading.Thread(target=self._run, name="bb-writer", daemon=True)

    def log(self, src, msg, t=None):
        line = "%s %-8s %s\n" % (_ts(t), src, msg[:LINE_MAX])
        self._put("log", line)

    def tele(self, rec):
        self._put("tele", json.dumps(rec, separators=(",", ":")) + "\n")

    def _put(self, kind, line):
        try:
            self.q.put_nowait((kind, line))
        except queue.Full:
            self.dropped_queue += 1

    def start(self):
        self.thread.start()

    def _rotate(self, kind, limit, keep):
        path = self.paths[kind]
        try:
            if os.path.getsize(path) < limit:
                return
        except OSError:
            return
        self.files[kind].flush()
        self.files[kind].close()
        for i in range(keep, 0, -1):
            src = path if i == 1 else "%s.%d" % (path, i - 1)
            dst = "%s.%d" % (path, i)
            if os.path.exists(src):
                if i == keep and os.path.exists(dst):
                    os.remove(dst)
                os.replace(src, dst)
        self.files[kind] = open(path, "a", buffering=1 << 16, encoding="utf-8", errors="replace")

    def _flush(self):
        for f in self.files.values():
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
        self._rotate("log", LOG_ROTATE_BYTES, LOG_ROTATE_KEEP)
        self._rotate("tele", TELE_ROTATE_BYTES, TELE_ROTATE_KEEP)

    def _run(self):
        last_sync = _now()
        while not self.stop.is_set() or not self.q.empty():
            try:
                kind, line = self.q.get(timeout=0.25)
                self.files[kind].write(line)
            except queue.Empty:
                pass
            if _now() - last_sync >= FSYNC_EVERY:
                self._flush()
                last_sync = _now()
        self._flush()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        for f in self.files.values():
            try:
                f.close()
            except Exception:
                pass


class RateLimiter:
    def __init__(self, writer, src):
        self.w, self.src = writer, src
        self.sec, self.n, self.dropped = 0, 0, 0

    def emit(self, msg, t=None):
        s = int(_now())
        if s != self.sec:
            if self.dropped:
                self.w.log("BBOX", "dropped %d lines from %s (rate limit)" % (self.dropped, self.src))
            self.sec, self.n, self.dropped = s, 0, 0
        self.n += 1
        if self.n > RATE_LINES_PER_SEC:
            self.dropped += 1
            return
        self.w.log(self.src, ANSI_RE.sub("", msg.rstrip("\n")), t)


# ----------------------------------------------------------------- sources

def kmsg_thread(w, state, stop):
    """Kernel ring buffer (everything since boot, then live)."""
    try:
        fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
    except Exception as e:
        w.log("BBOX", "cannot open /dev/kmsg: %s" % e)
        return
    rl = RateLimiter(w, "KERN")
    boot_wall = _now() - _uptime()
    last_seq = int(state.get("kmsg_seq", -1))
    while not stop.is_set():
        try:
            data = os.read(fd, 16384)
        except BlockingIOError:
            stop.wait(0.3)
            continue
        except OSError as e:
            if e.errno == 32:  # EPIPE: records overwritten, continue from the oldest
                continue
            w.log("BBOX", "kmsg read error: %s" % e)
            stop.wait(2)
            continue
        try:
            head, _, body = data.decode("utf-8", "replace").partition(";")
            pri, seq, usec = head.split(",")[:3]
            seq = int(seq)
            if seq <= last_seq:
                continue
            last_seq = seq
            state["kmsg_seq"] = seq
            msg = body.split("\n", 1)[0]
            rl.emit("<%s> %s" % (int(pri) & 7, msg), boot_wall + int(usec) / 1e6)
        except Exception:
            continue


def journal_thread(w, state, stop):
    rl = RateLimiter(w, "JRNL")
    filt = ["_TRANSPORT=journal", "_TRANSPORT=syslog", "_TRANSPORT=stdout"]
    first = not state.get("journal_dumped")
    while not stop.is_set():
        cmd = ["journalctl", "-o", "short-iso", "--no-pager"]
        cmd += (["-b"] if first else ["-n", "0"]) + ["-f"] + filt
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 universal_newlines=True, errors="replace")
        except Exception as e:
            w.log("BBOX", "journalctl unavailable: %s" % e)
            return
        first = False
        state["journal_dumped"] = True
        try:
            for line in p.stdout:
                if stop.is_set():
                    break
                rl.emit(line)
        finally:
            try:
                p.kill()
            except Exception:
                pass
        stop.wait(5)


def filelog_thread(w, state, stop):
    """Follow plain log files (new ones from byte 0, pre-existing ones from the tail)."""
    files = {}   # path -> {ino, pos, rl}
    fresh = not state.get("files_started")
    state["files_started"] = True
    while not stop.is_set():
        paths = list(FILE_LOGS)
        for g in FILE_LOG_GLOBS:
            paths += glob.glob(g)
        for p in sorted(set(paths)):
            try:
                st = os.stat(p)
            except OSError:
                continue
            info = files.get(p)
            if info is None:
                name = "FILE:" + os.path.basename(p)
                start = 0 if fresh else max(0, st.st_size - 16384)
                info = files[p] = {"ino": st.st_ino, "pos": start, "rl": RateLimiter(w, name[:8] if len(name) > 8 else name)}
                info["rl"].src = name
            if st.st_ino != info["ino"] or st.st_size < info["pos"]:
                info["ino"], info["pos"] = st.st_ino, 0
            if st.st_size > info["pos"]:
                try:
                    with open(p, "rb") as f:
                        f.seek(info["pos"])
                        chunk = f.read(2 * 1024 * 1024)
                except Exception:
                    continue
                # keep only complete lines
                cut = chunk.rfind(b"\n")
                if cut < 0:
                    if len(chunk) < 65536:
                        continue
                    cut = len(chunk) - 1
                info["pos"] += cut + 1
                for line in chunk[:cut + 1].decode("utf-8", "replace").splitlines():
                    if line.strip():
                        info["rl"].emit(line)
        fresh = True   # files that show up later in this boot are read from the start
        stop.wait(1.0)


def _proc_dstate():
    n, names = 0, []
    try:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % d) as f:
                    s = f.read()
                comm = s[s.index("(") + 1:s.rindex(")")]
                state = s[s.rindex(")") + 2]
            except Exception:
                continue
            if state == "D":
                n += 1
                if len(names) < 4:
                    names.append("%s:%s" % (d, comm))
    except Exception:
        pass
    return n, names


def _meminfo():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable", "SwapFree", "Dirty", "Writeback"):
                    out[k] = int(v.split()[0]) // 1024
    except Exception:
        pass
    return out


def _cpu_temp():
    best = None
    for p in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
        try:
            v = int(_read(p)) / 1000.0
            best = v if best is None else max(best, v)
        except Exception:
            pass
    return best


def _net():
    out = {}
    try:
        for ifc in os.listdir("/sys/class/net"):
            if ifc == "lo":
                continue
            base = "/sys/class/net/%s/" % ifc
            rec = {"c": _read(base + "carrier", "?"), "o": _read(base + "operstate", "?")}
            for key, f in (("rx", "rx_bytes"), ("tx", "tx_bytes"), ("re", "rx_errors"), ("te", "tx_errors"),
                           ("rd", "rx_dropped")):
                rec[key] = int(_read(base + "statistics/" + f, "0") or 0)
            out[ifc] = rec
        with open("/proc/net/wireless") as f:
            for line in f.read().splitlines()[2:]:
                parts = line.replace(":", " ").split()
                if parts and parts[0] in out and len(parts) > 3:
                    out[parts[0]]["sig"] = parts[3].rstrip(".")
    except Exception:
        pass
    return out


def _khs():
    try:
        p = "/run/hive/khs"
        return {"v": float(_read(p, "0") or 0), "age": int(_now() - os.path.getmtime(p))}
    except Exception:
        return None


def telemetry_thread(w, stop):
    seq = 0
    while not stop.is_set():
        seq += 1
        dn, dnames = _proc_dstate()
        try:
            la = [round(x, 2) for x in os.getloadavg()]
        except Exception:
            la = []
        rec = {"t": round(_now(), 1), "n": seq, "up": int(_uptime()), "la": la, "mem": _meminfo(),
               "ct": _cpu_temp(), "d": dn, "net": _net(), "khs": _khs(),
               "run": os.path.exists("/run/hive/MINER_RUN"),
               "wdl": _read("/sys/class/watchdog/watchdog0/timeleft") or None}
        if dnames:
            rec["dn"] = dnames
        w.tele(rec)
        stop.wait(TELEMETRY_EVERY)


def probe_thread(w, stop):
    """nvidia-smi + gateway ping in their own thread: a hung GPU driver must
    show up as 'TIMEOUT' in the record, not freeze the telemetry heartbeat."""
    query = ("index,temperature.gpu,power.draw,clocks.sm,clocks.mem,utilization.gpu,fan.speed,"
             "clocks_throttle_reasons.active,pstate")
    hung = 0
    while not stop.is_set():
        rec = {"t": round(_now(), 1), "probe": 1}
        if shutil.which("nvidia-smi") and hung < 3:
            try:
                p = subprocess.Popen(["nvidia-smi", "--query-gpu=" + query, "--format=csv,noheader,nounits"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                try:
                    out, err = p.communicate(timeout=10)
                    rows = []
                    for line in out.strip().splitlines():
                        rows.append([x.strip() for x in line.split(",")])
                    rec["gpu"] = rows
                    if p.returncode != 0:
                        rec["gpu_err"] = (err or out).strip()[:200]
                    hung = 0
                except subprocess.TimeoutExpired:
                    p.kill()
                    hung += 1
                    rec["gpu"] = "TIMEOUT"
                    w.log("BBOX", "nvidia-smi TIMEOUT (GPU/driver not answering) x%d" % hung)
            except Exception as e:
                rec["gpu_err"] = str(e)[:200]
        elif hung >= 3:
            hung = max(0, hung - 1)   # retry slowly while the driver is stuck
            rec["gpu"] = "SKIPPED(hung)"
        try:
            out = subprocess.run(["ip", "route", "show", "default"], stdout=subprocess.PIPE,
                                 universal_newlines=True, timeout=3).stdout
            m = re.search(r"via (\S+)", out)
            if m:
                r = subprocess.run(["ping", "-c1", "-W1", m.group(1)], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, universal_newlines=True, timeout=4)
                mm = re.search(r"time=([\d.]+)", r.stdout)
                rec["gw"] = float(mm.group(1)) if mm else None
                rec["gwip"] = m.group(1)
            else:
                rec["gw"] = "noroute"
        except Exception:
            rec["gw"] = "err"
        w.tele(rec)
        stop.wait(PROBE_EVERY)


# ----------------------------------------------------------------- daemon

def _select_boot_dir():
    bid = _boot_id()
    boots = list_boots()
    for b in reversed(boots[-3:]):
        if read_meta(b).get("boot_id") == bid:
            return b, True
    seq = 1
    if boots:
        try:
            seq = int(os.path.basename(boots[-1]).split("_")[0]) + 1
        except ValueError:
            seq = len(boots) + 1
    name = "%04d_%s" % (seq, time.strftime("%Y%m%d-%H%M%S", time.localtime()))
    path = os.path.join(BOOTS, name)
    os.makedirs(path, exist_ok=True)
    return path, False


def run_daemon():
    os.makedirs(BOOTS, exist_ok=True)
    os.chmod(BASE, 0o755)
    boot_dir, reused = _select_boot_dir()
    meta_path = os.path.join(boot_dir, "meta.json")
    state_path = os.path.join(boot_dir, "state.json")
    meta = read_meta(boot_dir)
    if not reused:
        meta = {"boot_id": _boot_id(), "version": VERSION, "hostname": _read("/etc/hostname"),
                "kernel": os.uname().release,
                "boot_wall": round(_now() - _uptime(), 1), "started": round(_now(), 1),
                "uptime_at_start": round(_uptime(), 1),
                "wd_bootstatus": _read("/sys/class/watchdog/watchdog0/bootstatus") or None,
                "wd_identity": _read("/sys/class/watchdog/watchdog0/identity") or None,
                "daemon_starts": 0}
    meta["daemon_starts"] = int(meta.get("daemon_starts", 0)) + 1
    meta.pop("stop", None)
    write_json_atomic(meta_path, meta)
    try:
        with open(state_path) as f:
            state = json.load(f)
    except Exception:
        state = {}

    w = Writer(boot_dir)
    w.start()
    w.log("BBOX", "blackbox v%d started (boot %s, daemon start #%d, %s)" %
          (VERSION, meta["boot_id"][:8], meta["daemon_starts"], "continuing boot" if reused else "new boot"))
    stop = threading.Event()
    threads = [threading.Thread(target=fn, args=args, daemon=True, name=fn.__name__)
               for fn, args in ((kmsg_thread, (w, state, stop)), (journal_thread, (w, state, stop)),
                                (filelog_thread, (w, state, stop)), (telemetry_thread, (w, stop)),
                                (probe_thread, (w, stop)))]
    for t in threads:
        t.start()

    def on_term(signum, _frame):
        meta["stop"] = {"t": round(_now(), 1), "signal": signum}
        try:
            write_json_atomic(meta_path, meta)
        except Exception:
            pass
        w.log("BBOX", "received signal %d - orderly stop (system shutdown/reboot or service restart)" % signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    last_prune = 0.0
    while not stop.is_set():
        stop.wait(5)
        try:
            write_json_atomic(state_path, state)
            if _now() - last_prune > 600:
                last_prune = _now()
                gone = prune_boots(boot_dir)
                if gone:
                    w.log("BBOX", "pruned %d old boot folder(s)" % len(gone))
        except Exception as e:
            w.log("BBOX", "housekeeping error: %s" % e)
        if w.dropped_queue:
            w.log("BBOX", "writer queue overflow: %d lines lost" % w.dropped_queue)
            w.dropped_queue = 0
    time.sleep(0.5)
    w.close()


# ----------------------------------------------------------------- report

def _tail_text(paths, nbytes):
    """Last nbytes across the given files (newest last)."""
    chunks, need = [], nbytes
    for p in paths:
        try:
            size = os.path.getsize(p)
            with open(p, "rb") as f:
                f.seek(max(0, size - need))
                chunks.insert(0, f.read().decode("utf-8", "replace"))
            need -= min(size, need)
        except OSError:
            continue
        if need <= 0:
            break
    return "".join(chunks)


def _log_paths(boot_dir, base):
    cands = [os.path.join(boot_dir, base)] + [os.path.join(boot_dir, "%s.%d" % (base, i)) for i in range(1, 6)]
    return [p for p in cands if os.path.exists(p)]


def _last_telemetry(boot_dir, count=60):
    text = _tail_text(_log_paths(boot_dir, "telemetry.jsonl"), 400 * 1024)
    recs = []
    for line in text.splitlines()[-count * 3:]:
        try:
            recs.append(json.loads(line))
        except Exception:
            continue
    return recs


def build_report(boot_ref="prev", minutes=15):
    boots = list_boots()
    if not boots:
        return {"ok": False, "error": "no boots recorded yet"}
    cur_id = _boot_id()
    cur_idx = None
    for i, b in enumerate(boots):
        if read_meta(b).get("boot_id") == cur_id:
            cur_idx = i
    if boot_ref in ("prev", "previous"):
        idx = (cur_idx if cur_idx is not None else len(boots)) - 1
    elif boot_ref == "latest":
        idx = len(boots) - 1
    else:
        idx = next((i for i, b in enumerate(boots) if os.path.basename(b).startswith("%04d" % int(boot_ref))), -1)
    if idx < 0 or idx >= len(boots):
        return {"ok": False, "error": "no such boot (have %d recorded)" % len(boots),
                "boots": [os.path.basename(b) for b in boots]}
    bdir = boots[idx]
    meta = read_meta(bdir)
    nxt_meta = read_meta(boots[idx + 1]) if idx + 1 < len(boots) else {}

    tele = [r for r in _last_telemetry(bdir) if not r.get("probe")]
    probes = [r for r in _last_telemetry(bdir, 120) if r.get("probe")]
    log_text = _tail_text(_log_paths(bdir, "blackbox.log"), 3 * 1024 * 1024)
    log_lines = log_text.splitlines()
    last_tele_t = tele[-1]["t"] if tele else None
    last_log_t = None
    for line in reversed(log_lines):
        try:
            last_log_t = time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
            break
        except Exception:
            continue
    end_t = max([x for x in (last_tele_t, last_log_t, (meta.get("stop") or {}).get("t")) if x] or [meta.get("started", 0)])
    clean = bool(meta.get("stop"))
    is_current = idx == cur_idx
    started = meta.get("boot_wall") or meta.get("started")

    window_start = end_t - minutes * 60
    severe, warn_count = [], 0
    for line in log_lines:
        try:
            lt = time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if lt < window_start:
            continue
        if SEVERE_RE.search(line):
            severe.append(line[:300])
        elif re.search(r"error|fail|warn", line, re.I):
            warn_count += 1
    severe = severe[-40:]

    last = tele[-1] if tele else {}
    verdict = []
    wd_next = nxt_meta.get("wd_bootstatus")
    wd_reset = False
    try:
        wd_reset = bool(int(str(wd_next), 0) & 0x20) if wd_next not in (None, "") else False
    except ValueError:
        pass
    mem_av = (last.get("mem") or {}).get("MemAvailable")
    gpu_hang = any(p.get("gpu") == "TIMEOUT" for p in probes[-6:])
    d_states = [r.get("d", 0) for r in tele[-12:]]
    net_down = []
    for r in tele[-6:]:
        for ifc, v in (r.get("net") or {}).items():
            if ifc.startswith(("wl", "eth", "en")) and v.get("c") == "0" and v.get("o") == "up":
                net_down.append(ifc)
    gw_fail = [p.get("gw") for p in probes[-6:] if "gw" in p]
    gw_dead = len(gw_fail) >= 3 and all(g in (None, "err", "noroute") for g in gw_fail[-3:])
    max_gpu_t = 0
    for p in probes[-6:]:
        if isinstance(p.get("gpu"), list):
            for row in p["gpu"]:
                try:
                    max_gpu_t = max(max_gpu_t, float(row[1]))
                except Exception:
                    pass

    if is_current:
        verdict.append("This is the CURRENT boot (still running); use --boot prev for the previous one.")
    elif clean:
        verdict.append("Clean stop: the blackbox received SIGTERM, i.e. a normal shutdown/reboot "
                       "(sreboot, panel, net-heal, wd action, manual). See 'shutdown hints' below for who asked.")
    else:
        if wd_reset:
            verdict.append("HARDWARE WATCHDOG RESET: the next boot reports the previous reset was caused by the watchdog "
                           "timer (bootstatus=%s) - the system froze (kernel/PID1 stopped feeding /dev/watchdog)." % wd_next)
        else:
            verdict.append("UNCLEAN stop: no shutdown signal was recorded (power loss, PSU trip, hard hang, or a reset "
                           "the watchdog did not report; next-boot watchdog bootstatus=%s)." % wd_next)
        if gpu_hang:
            verdict.append("nvidia-smi stopped answering shortly before the end -> GPU/driver hang is the prime suspect.")
        if d_states and max(d_states) >= 3:
            verdict.append("Up to %d processes were stuck in uninterruptible sleep (D state) before the end -> "
                           "disk/driver I/O hang." % max(d_states))
        if mem_av is not None and mem_av < 150:
            verdict.append("Only %d MB RAM was available at the end -> memory exhaustion." % mem_av)
        if max_gpu_t >= 90 or (last.get("ct") or 0) >= 95:
            verdict.append("Very high temperature before the end (GPU %.0f C, CPU %s C) -> thermal event." %
                           (max_gpu_t, last.get("ct")))
        if net_down or gw_dead:
            verdict.append("Network was already down before the end (%s) - a network-dark rig, not necessarily a dead one." %
                           (", ".join(sorted(set(net_down))) or "gateway not answering"))
        if severe:
            verdict.append("%d suspicious kernel/system lines in the last %d min before the end (listed below)." %
                           (len(severe), minutes))
        if not (gpu_hang or severe or net_down or gw_dead or (d_states and max(d_states) >= 3) or
                (mem_av is not None and mem_av < 150) or max_gpu_t >= 90):
            verdict.append("Logs end ABRUPTLY with normal readings and no kernel messages: typical for sudden power loss "
                           "(PSU/PDU/mains), or a hard CPU/chipset lock-up that left no trace. Check power first.")

    hints = [l[:240] for l in log_lines[-3000:] if re.search(
        r"sreboot|Reached target (Reboot|Power-Off)|systemd-shutdown|Net-heal|reboot issued|shutdown|hive-watchdog.*(reboot|BARK)|"
        r"watchdog.*(reboot|reset)", l, re.I)][-12:]

    return {
        "ok": True, "boot": os.path.basename(bdir), "is_current": is_current, "clean_stop": clean,
        "started": started, "ended": end_t, "uptime_s": int(end_t - started) if started else None,
        "dark_gap_s": int(nxt_meta.get("boot_wall") - end_t) if (nxt_meta.get("boot_wall") and not clean) else None,
        "wd_bootstatus_next": wd_next, "wd_reset": wd_reset,
        "last_telemetry": last, "last_probe": probes[-1] if probes else None,
        "suspicious": severe, "warn_lines": warn_count, "verdict": verdict,
        "shutdown_hints": hints, "tail": log_lines[-30:],
        "boots_recorded": len(boots),
    }


def format_report(r):
    if not r.get("ok"):
        return "blackbox: %s" % r.get("error")
    f = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "?"
    out = ["=== blackbox report: boot %s ===" % r["boot"],
           "started : %s" % f(r["started"]),
           "ended   : %s  (%s)" % (f(r["ended"]), "clean stop" if r["clean_stop"] else "NO shutdown record"),
           "uptime  : %s min" % (r["uptime_s"] // 60 if r["uptime_s"] else "?")]
    if r.get("dark_gap_s") is not None:
        out.append("dark gap: %d min until the next boot" % (r["dark_gap_s"] // 60))
    out.append("")
    out.append("VERDICT:")
    out += ["  - " + v for v in r["verdict"]]
    lt = r.get("last_telemetry") or {}
    if lt:
        out += ["", "last telemetry (%s):" % f(lt.get("t")),
                "  load=%s  mem=%s  cpu_temp=%s  D-state=%s %s" % (lt.get("la"), lt.get("mem"), lt.get("ct"),
                                                               lt.get("d"), lt.get("dn", "")),
                "  hashrate=%s miner_run=%s wd_timeleft=%s" % (lt.get("khs"), lt.get("run"), lt.get("wdl")),
                "  net=%s" % json.dumps(lt.get("net"))]
    lp = r.get("last_probe")
    if lp:
        out.append("last probe (%s): gw=%s gpu=%s" % (f(lp.get("t")), lp.get("gw"), json.dumps(lp.get("gpu"))))
    if r["shutdown_hints"]:
        out += ["", "shutdown hints:"] + ["  " + h for h in r["shutdown_hints"]]
    if r["suspicious"]:
        out += ["", "suspicious lines before the end:"] + ["  " + s for s in r["suspicious"]]
    out += ["", "last log lines:"] + ["  " + s[:200] for s in r["tail"]]
    return "\n".join(out)


def status():
    boots = list_boots()
    total = _dir_size(BASE) if os.path.isdir(BASE) else 0
    cur = next((b for b in boots if read_meta(b).get("boot_id") == _boot_id()), None)
    return {"dir": BASE, "boots": len(boots), "total_mb": round(total / 1048576, 1),
            "current": os.path.basename(cur) if cur else None,
            "current_mb": round(_dir_size(cur) / 1048576, 1) if cur else 0,
            "version": VERSION, "max_mb": MAX_TOTAL_BYTES // 1048576}


def purge(everything=False):
    boots = list_boots()
    cur_id = _boot_id()
    removed = 0
    for b in boots:
        if not everything and read_meta(b).get("boot_id") == cur_id:
            continue
        shutil.rmtree(b, ignore_errors=True)
        removed += 1
    return removed


def main():
    ap = argparse.ArgumentParser(description="hive-blackbox flight recorder")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the recorder (systemd service)")
    sp = sub.add_parser("report", help="what happened to a previous boot")
    sp.add_argument("--boot", default="prev", help="prev | latest | <number>")
    sp.add_argument("--minutes", type=int, default=15)
    sp.add_argument("--json", action="store_true")
    sub.add_parser("status")
    pp = sub.add_parser("purge", help="delete recorded boots (default: all but the running one)")
    pp.add_argument("--all", action="store_true")
    sub.add_parser("uninstall", help="stop+remove the service and delete ALL recorded data")
    a = ap.parse_args()
    if a.cmd == "run":
        run_daemon()
    elif a.cmd == "report":
        r = build_report(a.boot, a.minutes)
        print(json.dumps(r, indent=1) if a.json else format_report(r))
    elif a.cmd == "status":
        print(json.dumps(status(), indent=1))
    elif a.cmd == "purge":
        print("removed %d boot folder(s)" % purge(a.all))
    elif a.cmd == "uninstall":
        subprocess.call("systemctl disable --now hive-blackbox 2>/dev/null", shell=True)
        for p in ("/etc/systemd/system/hive-blackbox.service", "/usr/local/lib/hive-blackbox"):
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        shutil.rmtree(BASE, ignore_errors=True)
        subprocess.call("systemctl daemon-reload", shell=True)
        print("uninstalled; %s removed" % BASE)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
