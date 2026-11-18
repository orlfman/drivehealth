"""More regression checks: SMART exit bits, watch mode identity and SMART cache, sudo reads, filesystem table."""
import glob as real_glob_mod
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch

PATH = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "drivehealth")
loader = importlib.machinery.SourceFileLoader("dh", PATH)
spec = importlib.util.spec_from_loader("dh", loader)
dh = importlib.util.module_from_spec(spec)
loader.exec_module(dh)
dh.USE_COLOR = False

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += not cond


# ── 1. SMART exit bits ──────────────────────────────────────────────────────
def smart_with(j, rc):
    dh.smartctl = lambda name: (j, rc, None)
    return dh.read_smart("sdx")


def ata(**kw):
    j = {"model_name": "Test SSD", "in_smartctl_database": True}
    j.update(kw)
    return j


i, _ = smart_with(ata(smart_status={"passed": True}), 16)
check("1a exit 16, passed, no table -> FAIL", i["status"] == "fail", i["status"])
check("1a has threshold note", any("threshold" in t for _, t in i["notes"]), i["notes"])

i, _ = smart_with(ata(ata_smart_attributes={"table": []}), 8)
check("1b exit 8, empty table, no verdict -> FAIL", i["status"] == "fail", i["status"])
check("1b note says verdict came from exit status",
      any("exit status" in t and "missing" in t for _, t in i["notes"]), i["notes"])

i, _ = smart_with(ata(smart_status={"passed": False}), 8)
overall = [t for _, t in i["notes"] if t.startswith("SMART overall")]
check("1c exit 8, failed JSON -> FAIL, one overall note",
      i["status"] == "fail" and len(overall) == 1 and "exit status" not in overall[0], i["notes"])

tbl = [{"id": 5, "name": "Reallocated_Sector_Ct", "value": 5, "thresh": 10,
        "when_failed": "now", "flags": {"prefailure": True}, "raw": {"value": 0, "string": "0"}}]
i, _ = smart_with(ata(smart_status={"passed": True}, ata_smart_attributes={"table": tbl}), 16)
fails_notes = [t for l, t in i["notes"] if l == "fail"]
check("1d exit 16 + JSON failing prefail attr -> one explanation",
      i["status"] == "fail" and len(fails_notes) == 1, fails_notes)

i, _ = smart_with({"model_name": "Bare"}, 8)
check("1e exit 8 with no SMART keys -> FAIL", i is not None and i["status"] == "fail",
      i and i["status"])

i, _ = smart_with(ata(smart_status={"passed": True}, ata_smart_attributes={"table": []}), 0)
check("1f exit 0, passed -> not FAIL", i["status"] != "fail", i["notes"])

i, _ = smart_with(ata(smart_status={"passed": True}), 8)
check("1g exit 8 but JSON passed -> FAIL, says JSON disagreed",
      i["status"] == "fail" and any("said passed" in t for _, t in i["notes"]), i["notes"])

nv = {"model_name": "NV", "smart_status": {"passed": False},
      "nvme_smart_health_information_log": {"critical_warning": 8}}
i, _ = smart_with(nv, 8)
check("1h NVMe read-only critical warning still FAIL", i["status"] == "fail")

i, _ = smart_with(ata(smart_status={"passed": True}), 0x40)
check("1i history-only bit 0x40 doesn't FAIL", i["status"] != "fail", i["notes"])

# reload to undo the smartctl patch
loader.exec_module(dh)
dh.USE_COLOR = False

# ── 3. ATA vendor counters gated on the drive database ─────────────────────
pend = [{"id": 197, "name": "Current_Pending_Sector", "value": 100, "thresh": 0,
         "raw": {"value": 12, "string": "12"}},
        {"id": 199, "name": "UDMA_CRC_Error_Count", "value": 100, "thresh": 0,
         "raw": {"value": 7, "string": "7"}},
        {"id": 174, "name": "Unexpect_Power_Loss_Ct", "value": 100, "thresh": 0,
         "raw": {"value": 3, "string": "3"}}]
base = {"model_name": "Mystery SSD", "smart_status": {"passed": True},
        "ata_smart_attributes": {"table": pend}}
i = dh.parse_ata(dict(base, in_smartctl_database=False)); dh.evaluate(i)
check("3a unknown model: pending sectors not a WARN", i["status"] == "ok", i["notes"])
check("3a unknown model: media/unsafe unknown", i["media"] is None and i["unsafe"] is None,
      (i["media"], i["unsafe"]))
check("3a unknown model: no CRC note", not any("CRC" in t for _, t in i["notes"]))
check("3a unknown model: explains why", any("database" in t for _, t in i["notes"]))
i = dh.parse_ata(dict(base, in_smartctl_database=True)); dh.evaluate(i)
check("3b known model: 12 pending -> WARN", i["status"] == "warn" and i["media"] == 12, i["notes"])
check("3b known model: unsafe = 3", i["unsafe"] == 3)
ds = {"pages": [{"table": [{"name": "Number of Reallocated Logical Sectors", "value": 4},
                           {"name": "Number of Interface CRC Errors", "value": 2}]}]}
i = dh.parse_ata(dict(base, in_smartctl_database=False, ata_device_statistics=ds)); dh.evaluate(i)
check("3c unknown model: standard devstat still counted",
      i["media"] == 4 and any("2 interface CRC" in t for _, t in i["notes"]), (i["media"], i["notes"]))
fl = [{"id": 197, "name": "Current_Pending_Sector", "value": 1, "thresh": 10,
       "when_failed": "now", "flags": {"prefailure": True}, "raw": {"value": 0, "string": "0"}}]
i = dh.parse_ata(dict(base, in_smartctl_database=False, ata_smart_attributes={"table": fl}))
dh.evaluate(i)
check("3d unknown model: explicit failure flag still FAIL", i["status"] == "fail")

# ── 5. primary kernel temperature range ────────────────────────────────────
with tempfile.TemporaryDirectory() as T:
    h = os.path.join(T, "hwmon0")
    os.makedirs(h)

    def put(n, v):
        with open(os.path.join(h, n), "w") as f:
            f.write(str(v))

    put("temp1_input", 255000); put("temp1_label", "Composite")
    put("temp1_max", 84850); put("temp2_input", 41000)
    fake = types.SimpleNamespace(glob=lambda p: real_glob_mod.glob(
        p.replace("/sys/block/fake0/device", T)))
    dh.glob = fake
    d = dh.Drive("fake0", "M", None, "x")
    d.info = dict(temp=39, sensors=[], warn=None, crit=None, time_hot=None)
    th = dh.thermals(d)
    check("5a 255 °C kernel reading -> SMART 39 °C kept", th["temp"] == 39, th)
    check("5a limits still read", th["warn"] == 85, th)
    put("temp1_input", 41000)
    th = dh.thermals(d)
    check("5b valid kernel reading still wins", th["temp"] == 41, th)
    # watch peak uses therm temp: render with the bad reading
    put("temp1_input", 255000)
    d.therm = dh.thermals(d)
    d.info = None
    w = dh.Watcher(False, 1)
    w.drives = [d]
    w.frame()
    check("5c peak not set from bad reading", w.peaks.get(d.ident) == 39, w.peaks)
    dh.glob = real_glob_mod

# ── 6. one path per multi-mounted filesystem ───────────────────────────────
real_run = subprocess.run


def lsblk_with(mps):
    out = {"blockdevices": [{"name": "fake0", "type": "disk", "size": 100, "model": "M",
                             "serial": "S", "children": [
                                 {"name": "fake0p1", "type": "part", "size": 90, "label": None,
                                  "mountpoints": mps, "fssize": 90, "fsused": 10, "fsavail": 80}]}]}
    return types.SimpleNamespace(stdout=json.dumps(out))


for order in (["/z/x", "/a/a"], ["/a/a", "/z/x"]):
    dh.subprocess = types.SimpleNamespace(run=lambda *a, **k: lsblk_with(order),
                                          TimeoutExpired=subprocess.TimeoutExpired,
                                          CalledProcessError=subprocess.CalledProcessError)
    d = dh.find_drives()[0]
    check(f"6 mounts {order}: I/O and usage agree on /a/a",
          d.mount == "/a/a" and d.filesystems[0][0] == "/a/a", (d.mount, d.filesystems))
# "/" still beats everything, /home next
dh.subprocess.run = lambda *a, **k: lsblk_with(["/srv/bind", "/home"])
d = dh.find_drives()[0]
check("6 /home preferred over a bind path", d.mount == "/home" and d.label == "home", (d.mount, d.label))
dh.subprocess = subprocess

# ── 4. lsblk timeout ───────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as B:
    with open(os.path.join(B, "lsblk"), "w") as f:
        f.write("#!/bin/sh\nexec sleep 30\n")
    os.chmod(os.path.join(B, "lsblk"), 0o755)
    old = os.environ["PATH"]
    os.environ["PATH"] = B + ":" + old
    dh.LSBLK_TIMEOUT = 1
    try:
        dh.find_drives()
        check("4 hung lsblk times out", False)
    except subprocess.TimeoutExpired as e:
        check("4 hung lsblk times out", True)
        check("4 specific diagnostic", "didn't answer within 1 s" in dh.discovery_error(e),
              dh.discovery_error(e))
    os.environ["PATH"] = old

# ── 2 + 7. watch mode: identity after failed scans, first failed scan ──────
loader.exec_module(dh)
dh.USE_COLOR = False
dev = {"seq": 1, "temp": 35, "model": "Old", "io": (1000, 1000, 10)}
disc = {"ok": True}


def fake_find():
    if not disc["ok"]:
        raise subprocess.CalledProcessError(1, "lsblk")
    d = dh.Drive("sdq", dev["model"], "/q", "qdisk", "SER" + dev["model"], dev["seq"])
    d.filesystems = [("/q", 100, 10, 90)]
    return [d]


dh.find_drives = fake_find
dh.disk_seq = lambda name: dev["seq"]
dh.thermals = lambda d: dict(temp=dev["temp"], sensors=[], warn=None, crit=None, time_hot=None)
dh.diskstats = lambda: {"sdq": dev["io"]}

# 7: discovery fails on the very first round
disc["ok"] = False
w = dh.Watcher(False, 1)
w.refresh()
txt = "\n".join(w.frame())
check("7 first failed scan: says inventory unavailable", "Drive inventory unavailable" in txt, txt)
check("7 first failed scan: no 'last good scan'", "last good scan" not in txt)
check("7 first failed scan: no empty-filesystem message", "No mounted filesystems" not in txt)

# 2: good scan, then failure + replacement under the same name, then good scan
disc["ok"] = True
w = dh.Watcher(False, 1)
w.refresh(); w.frame()
old = w.drives[0]
check("2 setup: old peak 35", w.peaks.get(old.ident) == 35)
disc["ok"] = False
dev.update(seq=2, temp=55, model="New", io=(5000, 5000, 20))
w.refresh()
txt = "\n".join(w.frame())
check("2 failed scan + replacement: old drive frozen", old.frozen)
check("2 failed scan + replacement: old peak stays 35", w.peaks.get(old.ident) == 35, w.peaks)
check("2 failed scan + replacement: old therm not overwritten", old.therm["temp"] == 35, old.therm)
check("2 stale + frozen banner shown", "last good scan" in txt and "same disk" in txt, txt)
check("2 frozen drive has no boot I/O (new disk's counters hidden)",
      "4.9 kB" not in txt and "5.0 kB" not in txt, txt)
dev["io"] = (6000, 6000, 30)
w.refresh(); w.frame()
check("2 second failed round: still frozen, peak 35", old.frozen and w.peaks.get(old.ident) == 35)
disc["ok"] = True
dev["io"] = (7000, 7000, 40)
w.refresh()
live = dh.rates(w.prev, dh.diskstats(), 1)
txt = "\n".join(w.frame())
new = w.drives[0]
check("2 first good scan after replacement: new drive, own peak 55",
      new.ident != old.ident and w.peaks.get(new.ident) == 55 and w.peaks.get(old.ident) == 35)
check("2 first good scan: stale banner gone", "last good scan" not in txt)
check("2 first good scan: no rate across devices", "READ NOW" in txt and "kB/s" not in txt, txt)

# same disk, lsblk hiccup: keeps updating
w = dh.Watcher(False, 1)
w.refresh(); w.frame()
d0 = w.drives[0]
disc["ok"] = False
dev["temp"] = 60
w.refresh(); w.frame()
check("2 failed scan, same disk: not frozen, peak updates to 60",
      not d0.frozen and w.peaks.get(d0.ident) == 60, (d0.frozen, w.peaks))


# Exercise the real SMART decoder, including raw bytes and signal termination.
loader.exec_module(dh)
dh.USE_COLOR = False

for rc, stdout, should_fail in (
        (8, b"", True), (16, b"{", True), (24, b"\xff", True),
        (16, b"[]", True), (0, b"", False), (2, b"", False),
        (64, b"{", False), (-4, b"{}", False)):
    proc = subprocess.CompletedProcess(["smartctl"], rc, stdout, b"")
    with patch.object(subprocess, "run", return_value=proc):
        try:
            i, err = dh.read_smart("fake")
        except UnicodeDecodeError as e:
            check(f"raw SMART output {stdout!r}, exit {rc}: decoder raised", False, str(e))
            continue
    check(f"raw SMART output {stdout!r}, exit {rc}",
          (i is not None and i["status"] == "fail" and any("no usable output" in msg for _, msg in i["notes"]))
          if should_fail else (i is None and bool(err)))

def swap_during_round(stale, timing, use_seq=True):
    # The two disks use the same kernel name and model and have no serial.
    disk = dict(seq=1, temp=66, io=(100, 100, 1))
    scan = dict(fail=False, armed=False)

    def swap():
        disk.update(seq=2, temp=32, io=(9000000, 9000000, 50))

    def find():
        if scan["fail"]:
            raise subprocess.CalledProcessError(1, "lsblk")
        return [dh.Drive("sdq", "same model", None, "sample",
                         seq=disk["seq"] if use_seq else None)]

    def thermal(d):
        if scan["armed"] and timing == "collection":
            swap()
        return dict(temp=disk["temp"], sensors=[], warn=None, crit=None, time_hot=None)

    def stats():
        if scan["armed"] and timing == "diskstats":
            swap()
        return {"sdq": disk["io"]}

    with patch.object(dh, "find_drives", find), patch.object(dh, "diskstats", stats), \
            patch.object(dh, "thermals", thermal), \
            patch.object(dh, "disk_seq", lambda _: disk["seq"] if use_seq else None):
        w = dh.Watcher(False, 2)
        w.refresh()
        w.frame()
        scan.update(fail=stale, armed=True)
        w.refresh()
        if timing == "before_frame":
            swap()
        lines = w.frame()
        old = w.drives[0]
        desc = f"{timing}, stale={stale}, diskseq={use_seq}"
        check(desc + ": old measurements retained and replacement I/O suppressed",
              old.frozen and old.therm["temp"] == 66 and w.peaks[old.ident] == 66
              and "sdq" not in w.prev and not any("9.0 MB" in l for l in lines))
        if timing == "after_freeze":
            swap()
        scan.update(fail=False, armed=False)
        w.refresh()
        lines = w.frame()
        d = w.drives[0]
        io_row = next(l for l in lines if "9.0 MB" in l)
        check(desc + ": first recovered sample starts at 32 C without a rate",
              not d.frozen and w.peaks[d.ident] == 32 and "/s" not in io_row
              and "%" not in io_row and w.prev["sdq"] == disk["io"])
        disk["io"] = (11000000, 11000000, 100)
        w.refresh()
        lines = w.frame()
        check(desc + ": rates resume after a second verified sample",
              any("11.0 MB" in l and "/s" in l for l in lines))

for stale in (True, False):
    for timing in ("collection", "before_frame", "diskstats"):
        swap_during_round(stale, timing)
swap_during_round(True, "after_freeze", use_seq=False)


# Run the public CLI through discovery and collection with a recorded command
# runner. No host credentials or SMART hardware are used by these checks.
loader.exec_module(dh)
def cli_commands(uid, flags=(), terminal=False, sudo="/usr/bin/sudo", denied=False):
    commands = []
    def record(cmd, **kwargs):
        commands.append(tuple(cmd))
        if cmd[0] == "lsblk":
            data = {"blockdevices": [{"name": "nvme0n1", "type": "disk", "model": "N"},
                                     {"name": "sda", "type": "disk", "model": "A"}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")
        if cmd == [sudo, "-v"]:
            return subprocess.CompletedProcess(cmd, 0)
        if denied:
            return subprocess.CompletedProcess(cmd, 1, b"", b"sudo: a password is required\n")
        return subprocess.CompletedProcess(cmd, 0, b'{"smart_status":{"passed":true}}', b"")
    with patch.object(os, "geteuid", return_value=uid), \
            patch.object(os, "execvp", side_effect=AssertionError("dashboard re-exec")), \
            patch.object(dh, "SUDO", sudo), patch.object(dh, "SMARTCTL", "/usr/sbin/smartctl"), \
            patch.object(subprocess, "run", side_effect=record), \
            patch.object(sys, "argv", [PATH, "--no-color", *flags]), \
            patch.object(sys.stdin, "isatty", return_value=terminal), \
            patch.object(sys.stderr, "isatty", return_value=False), \
            patch.object(dh.time, "sleep"), patch("builtins.print") as output:
        dh.main()
    return commands, output.call_args.args[0]

cmds, rendered = cli_commands(1000, terminal=True)
check("one foreground credential check", cmds.count(("/usr/bin/sudo", "-v")) == 1)
reads = [cmd for cmd in cmds if cmd[-1] in ("/dev/sda", "/dev/nvme0n1")]
check("both SMART reads use only sudo -n and the system smartctl",
      len(reads) == 2 and all(cmd[:4] == ("/usr/bin/sudo", "-n", "--", "/usr/sbin/smartctl") for cmd in reads))
check("SMART query options are preserved for both protocols",
      any(cmd[4:] == ("--json", "-H", "-i", "-A", "/dev/nvme0n1") for cmd in reads)
      and any(cmd[4:] == ("--json", "-H", "-i", "-A", "-l", "error", "-l", "devstat", "-n", "standby", "/dev/sda") for cmd in reads))
check("discovery stays outside sudo", any(cmd[0] == "lsblk" for cmd in cmds) and len(cmds) == 4)
check("privileged reads still reach the summary", "all healthy" in rendered)
cmds, rendered = cli_commands(1000, denied=True)
check("redirected run never prompts", ("/usr/bin/sudo", "-v") not in cmds)
check("denied SMART explains why and keeps other sections",
      "a password is required" in rendered and "READS & WRITES" in rendered and "FILESYSTEM USAGE" in rendered)
cmds, rendered = cli_commands(1000, sudo=None, terminal=True)
check("missing sudo preserves the report", len(cmds) == 1 and "sudo not installed" in rendered)
for uid in (0, 1000):
    cmds, rendered = cli_commands(uid, ["--no-sudo"], terminal=True)
    check(f"--no-sudo uid {uid} uses only discovery",
          len(cmds) == 1 and cmds[0][0] == "lsblk" and "SMART skipped (--no-sudo)" in rendered)
cmds, rendered = cli_commands(0, terminal=True)
check("root invokes smartctl directly", sum(cmd[0] == "/usr/sbin/smartctl" for cmd in cmds) == 2
      and all(cmd[0] != "/usr/bin/sudo" for cmd in cmds))

# Filesystem usage section: bar, colours, drive column, totals (2026-11-18 restyle)
loader.exec_module(dh)
dh.USE_COLOR = False
check("usage bar width and fill", dh.usage_bar(50).count("━") == 10 and len(dh.usage_bar(50)) == 20)
check("usage bar: tiny use shows one cell, 0% none, None dashed",
      dh.usage_bar(1).count("━") == 1 and dh.usage_bar(0).count("━") == 0 and "━" not in dh.usage_bar(None))
check("usage colour thresholds", [dh.usage_paint(p) for p in (69, 70, 85, 95)] ==
      [dh.green, dh.yellow, dh.orange, dh.red])
T = 2**40
fa = dh.Drive("sda", "M", "/", "root"); fa.filesystems = [("/", T, T // 4, 3 * T // 4), ("/lvm", T, None, None)]
fb = dh.Drive("sdb", "M", "/x", "big"); fb.filesystems = [("/lvm", T, None, None)]
for d in (fa, fb):
    d.therm = dict(temp=None, sensors=[], warn=None, crit=None, time_hot=None)
out = dh.render([fa, fb], {}, {}, 1, {}, False, False)
rows = {l.split()[0]: l for l in out if l.startswith("  /")}
check("usage row has drive, bar and df-style percent",
      "root" in rows["/"] and "━━━━━─" in rows["/"] and rows["/"].rstrip().endswith("25%"), rows.get("/"))
check("spanning volume lists both drives, missing values dashed",
      "root+big" in rows["/lvm"] and rows["/lvm"].rstrip().endswith("—"), rows.get("/lvm"))
check("usage totals line", any("2 filesystems · 2.0 TiB total · — used · — available" in l
                               for l in out), [l for l in out if "filesystems" in l])

# Partial columns stay unknown regardless of row order. Preserve measured zeros.
def footer(left, right):
    fa.filesystems = [("/", *left)]
    fb.filesystems = [("/x", *right)]
    lines = dh.render([fa, fb], {}, {}, 1, {}, False, False)
    return next(line.strip() for line in lines if "2 filesystems ·" in line)

for label, left, right, expected in (
        ("missing size only", (None, 0, 2), (10, 0, 8), "— total · 0 B used · 10 B available"),
        ("missing used only", (10, 2, 8), (10, None, 3), "20 B total · — used · 11 B available"),
        ("missing available only", (10, 2, None), (10, 3, 7), "20 B total · 5 B used · — available"),
        ("all unknown", (None, None, None), (None, None, None), "— total · — used · — available"),
        ("measured zeros", (10, 0, 0), (10, 0, 0), "20 B total · 0 B used · 0 B available")):
    check(label, footer(left, right) == "2 filesystems · " + expected)

# Discovery labels both backing disks after their shared logical volume.
vol = dict(name="vg-data", type="lvm", label="pool", mountpoints=["/data"],
           size=T, fssize=T, fsused=0, fsavail=T)
rootfs = dict(name="sdc1", type="part", label="root", mountpoints=["/"],
              size=T, fssize=T, fsused=0, fsavail=T)
tree = [dict(name=name, type="disk", children=children)
        for name, children in (("sda", [vol, vol]), ("sdb", [vol]), ("sdc", [rootfs]))]
scan = subprocess.CompletedProcess(["lsblk"], 0, json.dumps({"blockdevices": tree}), "")
with patch.object(subprocess, "run", return_value=scan), patch.object(dh, "disk_seq", return_value=None):
    backing = dh.find_drives()
for drive in backing:
    drive.therm = dict(temp=None, sensors=[], warn=None, crit=None, time_hot=None)
table = dh.render(backing, {}, {}, 1, {}, False, False)
data_rows = [line.strip() for line in table if line.startswith("  /data ")]
check("shared volume identifies each disk once despite identical labels and repeated nodes",
      len(data_rows) == 1 and "pool (sda)+pool (sdb)" in data_rows[0]
      and data_rows[0].count("pool") == 2)
check("shared volume counts once in complete totals",
      any("2 filesystems · 2.0 TiB total · 0 B used · 2.0 TiB available" in l for l in table))

# Sudo follow-ups (2026-11-18): SMART cadence, stdin, failed sudo -v, keepalive
loader.exec_module(dh)
dh.USE_COLOR = False
clock = {"t": 1000.0}
reads = []
cur = {"seq": 1}
def fake_read(name):
    reads.append(name)
    return dict(status="ok", notes=[], model="M", temp=None, sensors=[], warn=None, crit=None,
                time_hot=None), None
with patch.object(dh, "read_smart", fake_read), patch.object(dh.time, "monotonic", lambda: clock["t"]), \
        patch.object(dh, "find_drives", lambda: [dh.Drive("sdq", "M", None, "q", seq=cur["seq"])]), \
        patch.object(dh, "disk_seq", lambda n: cur["seq"]), patch.object(dh, "diskstats", lambda: {}), \
        patch.object(dh, "hwmon_temps", lambda n: None):
    w = dh.Watcher(True, 2)
    for _ in range(16):          # 32 s of 2 s frames
        w.refresh(); lines = w.frame(); clock["t"] += 2
    check("watch reads SMART once per 30 s, not every frame", len(reads) == 2, len(reads))
    check("watch footer says SMART cadence", any("SMART every 30 s" in l for l in lines))
    cur["seq"] = 2               # replaced disk: fresh read straight away
    w.refresh(); w.frame()
    check("new diskseq bypasses the SMART cache", len(reads) == 3, len(reads))
    w2 = dh.Watcher(True, 60)
    reads.clear()
    for _ in range(3):
        w2.refresh(); lines = w2.frame(); clock["t"] += 60
    check("intervals over 30 s read every frame, footer has no cadence",
          len(reads) == 3 and not any("SMART every" in l for l in lines))

seen = []
with patch.object(subprocess, "run", side_effect=lambda cmd, **kw: seen.append(kw) or
                  subprocess.CompletedProcess(cmd, 0, b"{}", b"")), \
        patch.object(dh, "SMARTCTL", "/usr/sbin/smartctl"), patch.object(dh, "SUDO", "/usr/bin/sudo"):
    dh.smartctl("sda")
check("smartctl gets /dev/null on stdin (no pty per sudo read)", seen and seen[0].get("stdin") == subprocess.DEVNULL)

def cli_v(rc, reads_ok=False, watch=None):
    calls = []
    def record(cmd, **kw):
        calls.append(tuple(cmd))
        if cmd[0] == "lsblk":
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"blockdevices": [
                {"name": "nvme0n1", "type": "disk", "model": "N"}, {"name": "sda", "type": "disk", "model": "A"}]}), "")
        if cmd == ["/usr/bin/sudo", "-v"]:
            return subprocess.CompletedProcess(cmd, rc)
        if reads_ok:
            return subprocess.CompletedProcess(cmd, 0, b'{"smart_status":{"passed":true}}', b"")
        return subprocess.CompletedProcess(cmd, 1, b"", b"sudo: a password is required\n")
    with patch.object(os, "geteuid", return_value=1000), patch.object(dh, "SUDO", "/usr/bin/sudo"), \
            patch.object(dh, "SMARTCTL", "/usr/sbin/smartctl"), patch.object(subprocess, "run", side_effect=record), \
            patch.object(sys, "argv", [PATH, "--no-color"]), patch.object(sys.stdin, "isatty", return_value=True), \
            patch.object(dh.time, "sleep"), patch("builtins.print") as output:
        dh.main()
    return calls, output.call_args.args[0]
calls, text = cli_v(1)
check("failed sudo -v: reads still tried, refusal said once",
      sum("/usr/sbin/smartctl" in c for c in calls) == 2 and "SMART skipped (sudo refused)" in text
      and "enter the sudo password" in text and text.count("password is required") == 1, text[:400])
loader.exec_module(dh); dh.USE_COLOR = False
calls, text = cli_v(1, reads_ok=True)
check("failed sudo -v but smartctl allowed (NOPASSWD + verifypw): drives read OK",
      "all healthy" in text and "sudo refused" not in text, text[:300])
loader.exec_module(dh); dh.USE_COLOR = False
calls, text = cli_v(0)
check("denied sudo -n on every drive is noted once", text.count("a password is required") == 1
      and "all drives: SMART:" in text, text.count("a password is required"))
loader.exec_module(dh); dh.USE_COLOR = False

# Cadence is counted in frames, so the footer states what really happens.
def read_times(interval, seq, frames, refuse=False):
    times, clock = [], {"t": 0.0}
    def fake_read(name):
        times.append(clock["t"])
        if refuse:
            return None, dh.SUDO_REFUSED + ": sudo: a password is required"
        return dict(status="ok", notes=[], model="M", temp=None, sensors=[], warn=None, crit=None,
                    time_hot=None), None
    with patch.object(dh, "read_smart", fake_read), \
            patch.object(dh, "find_drives", lambda: [dh.Drive("sdq", "M", None, "q", "S", seq)]), \
            patch.object(dh, "disk_seq", lambda n: seq), patch.object(dh, "diskstats", lambda: {}), \
            patch.object(dh, "hwmon_temps", lambda n: None):
        w = dh.Watcher(True, interval)
        for _ in range(frames):
            w.refresh(); lines = w.frame(); clock["t"] += interval
    foot = next(l for l in lines if "refreshing every" in l)
    return times, foot
for interval, seq, frames, want, foot_want in (
        (2, 1, 46, [0, 30, 60, 90], "SMART every 30 s"),
        (29, 1, 4, [0, 29, 58, 87], None),
        (2, None, 31, [0, 30, 60], "SMART every 30 s"),
        (7, 1, 9, [0, 28, 56], "SMART every 28 s"),
        (12, 1, 7, [0, 36, 72], "SMART every 36 s")):
    times, foot = read_times(interval, seq, frames)
    check(f"-w {interval} diskseq={seq}: reads at {want}", times == want, times)
    check(f"-w {interval} diskseq={seq}: footer matches", (foot_want in foot) if foot_want
          else "SMART every" not in foot, foot)
times, foot = read_times(2, 1, 40, refuse=True)
check("watch stops asking sudo after it refused every drive", times == [0], times)
check("refused watch footer drops the cadence", "SMART every" not in foot, foot)

ev, kcalls = dh.threading.Event(), []
waits = iter([False, False, True])
ev.wait = lambda t: next(waits)
with patch.object(dh, "SUDO", "/usr/bin/sudo"), patch.object(subprocess, "run",
        side_effect=lambda cmd, **kw: kcalls.append((cmd, kw.get("stdin"))) or subprocess.CompletedProcess(cmd, 0)):
    dh.sudo_keepalive(ev)
check("keepalive renews with sudo -n -v on /dev/null until stopped",
      kcalls == [(["/usr/bin/sudo", "-n", "-v"], subprocess.DEVNULL)] * 2, kcalls)
ev, kcalls = dh.threading.Event(), []
waits = iter([False, False, False, True])
ev.wait = lambda t: next(waits)
with patch.object(dh, "SUDO", "/usr/bin/sudo"), patch.object(subprocess, "run",
        side_effect=lambda cmd, **kw: kcalls.append(cmd) or subprocess.CompletedProcess(cmd, 1)):
    dh.sudo_keepalive(ev)
check("keepalive stops at the first failed renewal", len(kcalls) == 1, kcalls)


# Watcher regression checks at the discovery, collection and rendered-frame boundary.
def watch_samples(samples, interval=2):
    state, reads, frames = {}, [], []
    def discover():
        return [dh.Drive(name, "same model", None, name, serial, seq)
                for name, seq, serial, _ in state["disks"]]
    def sequence(name):
        return next(seq for n, seq, _, _ in state["disks"] if n == name)
    def read(name):
        reads.append((state["time"], name))
        health = next(v for n, _, _, v in state["disks"] if n == name)
        if health == "denied":
            return None, dh.SUDO_REFUSED + ": denied " + name
        info = dh.base_info({"smart_status": {"passed": health == "ok"}})
        dh.evaluate(info)
        return info, None
    with patch.object(dh, "find_drives", discover), patch.object(dh, "disk_seq", sequence), \
            patch.object(dh, "read_smart", read), patch.object(dh, "hwmon_temps", return_value=None), \
            patch.object(dh, "diskstats", return_value={}), \
            patch.object(dh.time, "clock_gettime", side_effect=lambda _: state["time"]):
        w = dh.Watcher(True, interval)
        for stamp, disks in samples:
            state.update(time=stamp, disks=disks)
            w.refresh()
            lines = w.frame()
            frames.append(({d.name: d.info["status"] if d.info else d.err for d in w.drives}, lines[-1]))
    return sorted(reads), frames

a = ("sda", 1, None, "ok")
b = ("sdb", 2, None, "fail")
denied = ("sda", 1, None, "denied")
reads, frames = watch_samples([(0, [("sda", None, None, "ok")]),
                              (2, [("sda", None, None, "fail")])])
check("unidentified replacement immediately shows its own failing verdict",
      reads == [(0, "sda"), (2, "sda")] and frames[-1][0]["sda"] == "fail")
check("unidentified disk footer reflects per-frame reads", "SMART every frame" in frames[0][1])
reads, frames = watch_samples([(0, [denied]), (2, [denied, b]), (4, [b])])
check("refused disk does not silence another attached disk",
      reads == [(0, "sda"), (2, "sdb")] and frames[1][0]["sdb"] == "fail"
      and "sda" in frames[1][0]["sda"] and frames[-1][0]["sdb"] == "fail")
reads, frames = watch_samples([(0, [denied]), (2, [("sda", 2, None, "ok")])])
check("same device name with a new attachment gets its own permission check",
      reads == [(0, "sda"), (2, "sda")] and frames[-1][0]["sda"] == "ok")
retry = 300
reads, frames = watch_samples([(0, [denied]), (2, [a]), (retry, [a])])
check("permission recovery is bounded without repeated attempts each frame",
      reads == [(0, "sda"), (retry, "sda")] and frames[-1][0]["sda"] == "ok")
reads, frames = watch_samples([(0, [a]), (2, [a]), (120, [("sda", 1, None, "fail")])])
check("first refresh after a long pause reads the current verdict",
      reads == [(0, "sda"), (120, "sda")] and frames[-1][0]["sda"] == "fail")
reads, frames = watch_samples([(0, [a]), (2, []), (4, [a])])
check("a drive absent for a frame gets a fresh read when it returns",
      reads == [(0, "sda"), (4, "sda")])
reads, frames = watch_samples([(0, [("sda", None, "serial-A", "ok")]),
                               (2, [("sda", None, "serial-B", "fail")])])
check("serial change bypasses cached health without a disk sequence",
      reads == [(0, "sda"), (2, "sda")] and frames[-1][0]["sda"] == "fail")


# A stall inside watch's sleep must expire old SMART data before the next display.
from io import StringIO
timeline = [0.0]
slept = []
read_stamps = []
def delayed_sleep(seconds):
    slept.append(seconds)
    if len(slept) > 2:
        raise KeyboardInterrupt
    if len(slept) == 2:
        timeline[0] = 120.0
def changing_health(_):
    read_stamps.append(timeline[0])
    info = dh.base_info({"smart_status": {"passed": timeline[0] == 0}})
    dh.evaluate(info)
    return info, None
screen = StringIO()
with patch.object(sys, "stdout", screen), patch.object(os, "geteuid", return_value=0), \
        patch.object(dh.time, "sleep", delayed_sleep), \
        patch.object(dh.time, "clock_gettime", side_effect=lambda _: timeline[0]), \
        patch.object(dh.time, "monotonic", side_effect=lambda: timeline[0]), \
        patch.object(dh, "find_drives", side_effect=lambda: [dh.Drive("sda", "M", None, "x", seq=1)]), \
        patch.object(dh, "disk_seq", return_value=1), patch.object(dh, "diskstats", return_value={}), \
        patch.object(dh, "hwmon_temps", return_value=None), patch.object(dh, "read_smart", changing_health):
    dh.watch(True, 2)
check("first displayed frame after a delayed sleep has the fresh FAIL",
      read_stamps == [0.0, 120.0] and "1 failing" in screen.getvalue().split("Drive health")[-1])

# Refused watch footer keeps the "what to do" hint next to the retry line
loader.exec_module(dh); dh.USE_COLOR = False
rd = dh.Drive("sdq", "M", None, "q", "S", 1)
rd.info, rd.err = None, dh.SUDO_REFUSED + ": sudo: a password is required"
rd.therm = dict(temp=None, sensors=[], warn=None, crit=None, time_hot=None)
foot = dh.render([rd], {}, {}, 2, {}, True, True)
check("refused watch footer has the password hint and the retry line",
      any("run again and enter the sudo password" in l for l in foot)
      and any("retry after 300 s" in l for l in foot))

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
