#!/usr/bin/env python3
"""Regression tests for drivehealth, using fake smartctl output.
Needs no root and touches no drive:  python3 -I tests/test_drivehealth.py [path to drivehealth]"""
import importlib.machinery, importlib.util, json, os, sys, types

SCRIPT = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "drivehealth")
loader = importlib.machinery.SourceFileLoader("dh", SCRIPT)
dh = importlib.util.module_from_spec(importlib.util.spec_from_loader("dh", loader))
loader.exec_module(dh)

FIX = {}


def fake_run(cmd, **kw):
    f = FIX[cmd[-1].split("/")[-1]]
    if isinstance(f, Exception):
        raise f
    rc, j = f
    return types.SimpleNamespace(returncode=rc, stdout=j if isinstance(j, str) else json.dumps(j))


real_run, dh.subprocess.run = dh.subprocess.run, fake_run


def attr(id, name, value=100, raw=0, thresh=0, string=None):
    a = dict(id=id, name=name, raw=dict(value=raw), thresh=thresh)
    if string is not None:
        a["raw"]["string"] = string
    if value is not None:
        a["value"] = value
    return a


def ata(*attrs, db=True, status={"passed": True}, **extra):
    j = dict(in_smartctl_database=db, ata_smart_attributes={"table": list(attrs)}, **extra)
    if status is not None:
        j["smart_status"] = status
    return j


def ds(**kv):
    return {"pages": [{"table": [{"name": k.replace("_", " "), "value": v} for k, v in kv.items()]}]}


NV = dict(critical_warning=0, percentage_used=0, available_spare=100, available_spare_threshold=10,
          media_errors=0, num_err_log_entries=0, unsafe_shutdowns=65, power_on_hours=8582,
          power_cycles=977, data_units_written=1, data_units_read=1, temperature=40)


def nvme(status={"passed": True}, **log):
    j = dict(nvme_smart_health_information_log=dict(NV, **log))
    if status is not None:
        j["smart_status"] = status
    return j


U = "unknown"
OCZ_IO = [attr(198, "Host_Reads_GiB", 100, 123), attr(199, "Host_Writes_GiB", 100, 456)]
FIX.update({
    # round 1
    "evo": (0, ata(attr(177, "Wear_Leveling_Count", 98, 25), attr(179, "Used_Rsvd_Blk_Cnt_Tot", 100, 0, 10),
                   attr(235, "POR_Recovery_Count", 99, 551), attr(241, "Total_LBAs_Written", 99, 6 * 10**10),
                   model_name="Samsung SSD 860 EVO 2TB",
                   ata_device_statistics=ds(Logical_Sectors_Written=60_937_500_000,
                                            Percentage_Used_Endurance_Indicator=1))),
    "partial": (4, ata(attr(5, "Reallocated_Sector_Ct"), status=None,
                       smartctl={"messages": [{"string": "Read SMART Data failed: Input/output error"}]})),
    "sleep": (2, dict(smartctl={"messages": [{"string": "Device is in STANDBY mode, exit(2)"}]})),
    "sandisk": (0, ata(attr(233, "Flash_Writes_GiB", 100, 5000), attr(241, "Lifetime_Writes_GiB", 100, 1),
                       attr(235, "Lifetime_Writes_GiB", 100, 1),
                       ata_device_statistics=ds(Percentage_Used_Endurance_Indicator=85))),
    "intel": (0, ata(attr(233, "Media_Wearout_Indicator", 40), attr(241, "Host_Writes_32MiB", 100, 1000),
                     attr(174, "Unexpect_Power_Loss_Ct", 100, 7))),
    "novalue": (0, ata(attr(177, "Wear_Leveling_Count", None), attr(179, "Used_Rsvd_Blk_Cnt_Tot", None), {"id": 9})),
    "generic": (0, ata(attr(233, "Media_Wearout_Indicator", 100), attr(241, "Total_LBAs_Written", 100, 2), db=False,
                       ata_device_statistics=ds(Percentage_Used_Endurance_Indicator=85))),
    "nv_ok": (0, nvme()),
    "nv_noverdict": (0, nvme(status=None)),
    "nv_fail": (8, nvme(status={"passed": False}, critical_warning=4)),
    "garbage": (1, "smartctl: unrecognized option"),
    "timeout": dh.subprocess.TimeoutExpired("smartctl", 20),
    "notadict": (0, [1, 2]),
    # round 2
    "ocz_io": (0, ata(attr(5, "Runtime_Bad_Block"), *OCZ_IO)),
    "realbad": (0, ata(attr(5, "Reallocated_Sector_Ct", 100, 3), attr(197, "Current_Pending_Sector", 100, 2),
                       attr(199, "UDMA_CRC_Error_Count", 100, 9))),
    "hrs": (0, dict(nvme(power_on_hours=U), power_on_time={"hours": U})),
    "tmp": (0, dict(nvme(temperature=U), temperature={"current": U})),
    "sens": (0, nvme(temperature_sensors=[41, U, None, 999])),
    "nvodd": (0, nvme(temperature_sensors=U, critical_comp_time=U, critical_warning=U, media_errors=U,
                      num_err_log_entries="3", data_units_written="x")),
    "ataodd": (0, ata(attr(5, "Reallocated_Sector_Ct", 100, U), logical_block_size="4096", power_cycle_count=U,
                      ata_smart_error_log={"summary": {"count": U}},
                      ata_device_statistics=ds(Logical_Sectors_Written=10, Time_in_Over_Temperature=U,
                                               Specified_Maximum_Operating_Temperature=U))),
    "p0": (0, nvme(status={"passed": 0})),
    "pstr": (0, nvme(status={"passed": "false"})),
    "p1": (0, nvme(status={"passed": 1})),
    "ssnotdict": (0, nvme(status="yes")),
    # round 3: same OCZ layout, but with real errors next to the I/O counters
    "ocz_err": (0, ata(attr(5, "Runtime_Bad_Block"), attr(197, "Total_Unc_Read_Failures", 100, 3),
                       attr(210, "SATA_CRC_Error_Count", 100, 7), *OCZ_IO)),
    "ocz_both": (0, ata(attr(187, "Total_Unc_NAND_Reads", 100, 2), attr(197, "Total_Unc_Read_Failures", 100, 3))),
    "crc218": (0, ata(attr(218, "CRC_Error_Count", 100, 4), attr(160, "Uncorrectable_Error_Cnt", 100, 1))),
    # round 4: SandForce packs errors/sectors into one raw value (raw24/raw32);
    # smartctl's formatted string is "errors/sectors"
    "sf_zero": (0, ata(attr(195, "ECC_Uncorr_Error_Count", 120, 100, string="0/100"),
                       attr(198, "Uncorrectable_Sector_Ct", 120, 100, string="0/100"))),
    "sf_three": (0, ata(attr(195, "ECC_Uncorr_Error_Count", 120, (3 << 32) | 100, string="3/100"),
                        attr(198, "Uncorrectable_Sector_Ct", 120, (2 << 32) | 100, string="2/100"))),
    "sf_nostring": (0, ata(attr(195, "ECC_Uncorr_Error_Count", 120, 100),
                           attr(198, "Uncorrectable_Sector_Ct", 120, (2 << 32) | 100))),
    # raw16(raw16): the count is the first number, the rest is extra words
    "r16": (0, ata(attr(5, "Reallocated_Sector_Ct", 100, (9 << 32) | 4, string="4 (9 0)"))),
    "r16_zero": (0, ata(attr(5, "Reallocated_Sector_Ct", 100, 9 << 32, string="0 (9 0)"))),
    # round 5: same layout with the formatted string missing, null or blank
    "r16_nostr_zero": (0, ata(attr(5, "Reallocated_Sector_Ct", 100, 9 << 32),
                              attr(5, "Realloc_Flash_Blocks_Ct", 100, 9 << 32))),
    "r16_nostr_four": (0, ata(attr(5, "Reallocated_Sector_Ct", 100, (9 << 32) | 4))),
    "r16_null": (0, ata(dict(attr(5, "Reallocated_Sector_Ct", 100), raw={"value": 9 << 32, "string": None}))),
    "r16_blank": (0, ata(attr(5, "Realloc_Flash_Blocks_Ct", 100, (9 << 32) | 6, string="  "))),
    "negraw": (0, ata(attr(5, "Retired_Block_Count", 100, -3), attr(199, "CRC_Error_Count", 100, -1))),
    # round 6: standard statistics beat vendor wear attributes...
    "wear230": (0, ata(attr(230, "Media_Wearout_Indicator", 100),
                       ata_device_statistics=ds(Percentage_Used_Endurance_Indicator=85))),
    "wear233": (0, ata(attr(233, "Media_Wearout_Indicator", 100),
                       ata_device_statistics=ds(Percentage_Used_Endurance_Indicator=85))),
    "wear230_only": (0, ata(attr(230, "Media_Wearout_Indicator", 100))),
    # ...except Samsung 177, where the higher of the two is shown
    "sam_std_higher": (0, ata(attr(177, "Wear_Leveling_Count", 98), model_name="Samsung SSD 870 EVO 1TB",
                              ata_device_statistics=ds(Percentage_Used_Endurance_Indicator=5))),
    "notsam177": (0, ata(attr(177, "Wear_Leveling_Count", 60), model_name="Other SSD",
                         ata_device_statistics=ds(Percentage_Used_Endurance_Indicator=1))),
    # standard error statistics count, without double counting the attributes
    "ds_err": (0, ata(attr(5, "Reallocated_Sector_Ct"), ata_device_statistics=ds(
        Number_of_Reported_Uncorrectable_Errors=7, Number_of_Interface_CRC_Errors=9))),
    "ds_overlap": (0, ata(attr(5, "Reallocated_Sector_Ct", 100, 2), attr(187, "Reported_Uncorrect", 100, 7),
                          attr(199, "UDMA_CRC_Error_Count", 100, 9), ata_device_statistics=ds(
        Number_of_Reported_Uncorrectable_Errors=7, Number_of_Interface_CRC_Errors=9,
        Number_of_Reallocated_Logical_Sectors=2))),
    "ds_clean": (0, ata(attr(5, "Reallocated_Sector_Ct"), ata_device_statistics=ds(
        Number_of_Reported_Uncorrectable_Errors=0, Number_of_Interface_CRC_Errors=0))),
    "verdict_only": (0, dict(smart_status={"passed": True}, device={"protocol": "ATA"})),
    # SCSI / SAS
    "sas_bad": (0, dict(device={"protocol": "SCSI", "type": "scsi"}, smart_status={"passed": True},
                        scsi_model_name="SEAGATE ST1200MM0009", scsi_grown_defect_list=3,
                        power_on_time={"hours": 100}, temperature={"current": 31, "drive_trip": 65},
                        scsi_error_counter_log={
                            "read": {"total_uncorrected_errors": 7, "gigabytes_processed": "123.456"},
                            "write": {"total_uncorrected_errors": 0, "gigabytes_processed": "2.5"}})),
    "sas_clean": (0, dict(device={"protocol": "SCSI"}, smart_status={"passed": True}, scsi_grown_defect_list=0,
                          scsi_error_counter_log={"read": {"total_uncorrected_errors": 0}},
                          endurance_used={"current_percent": 4})),
    "sas_bare": (0, dict(device={"protocol": "SCSI"}, smart_status={"passed": True})),
    "sas_noverdict": (0, dict(device={"protocol": "SCSI"}, scsi_grown_defect_list=0)),
    "sas_odd": (0, dict(device={"protocol": "SCSI"}, smart_status={"passed": True}, scsi_grown_defect_list=U,
                        scsi_error_counter_log={"read": U, "write": {"gigabytes_processed": "nan"}})),
    # a formatted value that isn't a count at all is left out, never guessed
    "oddfmt": (0, ata(attr(199, "CRC_Error_Count", 100, 77, string="0x00000000004d"),
                      attr(187, "Reported_Uncorrect", 100, 5, string="1h+02m+03.000s"),
                      attr(241, "Total_LBAs_Written", 100, 1000, string="1000"))),
})

GIB = 2**30
EXPECT = {  # name: fields that must match (status None = no data at all)
    "evo": dict(status="ok", wear=2, spare=100, unsafe=551, life_written=60_937_500_000 * 512),
    "partial": dict(status="unknown"),
    "sleep": None, "garbage": None, "timeout": None, "notadict": None,
    "sandisk": dict(status="warn", wear=85, unsafe=None, life_written=GIB),
    "intel": dict(status="ok", wear=60, unsafe=7, life_written=1000 * 32 * 2**20),
    "novalue": dict(status="ok", wear=None, spare=None),
    "generic": dict(status="warn", wear=85, life_written=None),
    "nv_ok": dict(status="ok", wear=0, unsafe=65),
    "nv_noverdict": dict(status="unknown"),
    "nv_fail": dict(status="fail"),
    "ocz_io": dict(status="ok", media=0, life_read=123 * GIB, life_written=456 * GIB, crc=False),
    "realbad": dict(status="warn", media=5, crc=True),
    "hrs": dict(status="ok", hours=None), "tmp": dict(status="ok", temp=None),
    "sens": dict(status="ok", sensors=[41]),
    "nvodd": dict(status="ok", sensors=[], media=None, errlog=None, life_written=None),
    "ataodd": dict(status="ok", media=None, cycles=None, errlog=None, time_hot=None, life_written=5120),
    "p0": dict(status="unknown"), "pstr": dict(status="unknown"), "p1": dict(status="unknown"),
    "ssnotdict": dict(status="unknown"),
    "ocz_err": dict(status="warn", media=3, life_read=123 * GIB, life_written=456 * GIB, crc=True),
    "ocz_both": dict(status="warn", media=3),
    "crc218": dict(status="warn", media=1, crc=True),
    "sf_zero": dict(status="ok", media=0),
    "sf_three": dict(status="warn", media=5),
    "sf_nostring": dict(status="warn", media=2),
    "r16": dict(status="warn", media=4),
    "r16_zero": dict(status="ok", media=0),
    "oddfmt": dict(status="ok", media=None, crc=False, life_written=512_000),
    "wear230": dict(status="warn", wear=85), "wear233": dict(status="warn", wear=85),
    "wear230_only": dict(status="ok", wear=None),
    "sam_std_higher": dict(status="ok", wear=5), "notsam177": dict(status="ok", wear=1),
    "ds_err": dict(status="warn", media=7, crc=True),
    "ds_overlap": dict(status="warn", media=9, crc=True),
    "ds_clean": dict(status="ok", media=0, crc=False),
    "verdict_only": dict(status="ok", media=None, wear=None),
    "sas_bad": dict(status="warn", media=10, hours=100, temp=31, crit=65, model="SEAGATE ST1200MM0009",
                    life_read=123_456_000_000, life_written=2_500_000_000),
    "sas_clean": dict(status="ok", media=0, wear=4),
    "sas_bare": dict(status="ok", media=None),
    "sas_noverdict": dict(status="unknown", media=0),
    "sas_odd": dict(status="ok", media=None, life_read=None, life_written=None),
    "r16_nostr_zero": dict(status="ok", media=0),
    "r16_nostr_four": dict(status="warn", media=4),
    "r16_null": dict(status="ok", media=0),
    "r16_blank": dict(status="warn", media=6),
    "negraw": dict(status="ok", media=None, crc=False),
}

fails = checks = 0


def check(what, got, want):
    global fails, checks
    checks += 1
    if got != want:
        fails += 1
        print(f"FAIL {what}: got {got!r}, want {want!r}")


assert set(FIX) == set(EXPECT)
for name, want in EXPECT.items():
    info, err = dh.read_smart(name)
    if want is None:
        check(f"{name} has no data", (info, bool(err)), (None, True))
        continue
    for k, v in want.items():
        got = any("CRC" in t for _, t in info["notes"]) if k == "crc" else info[k]
        check(f"{name}.{k}", got, v)
check("ocz_err note", [t for l, t in dh.read_smart("ocz_err")[0]["notes"] if l == "warn"],
      ["bad sectors: 3 uncorrectable"])

# one drive whose parse blows up must not hurt its neighbours, and every
# fixture must render next to good drives with colour on and off
FIX["boom"] = (0, ata("not an attribute", ata_device_statistics={"pages": [7]}))
for dh.USE_COLOR in (True, False):
    drives = [dh.Drive(n, "?", None, n) for n in ["nv_ok", "evo", "boom"] + sorted(set(FIX) - {"nv_ok", "evo", "boom"})]
    dh.collect(drives, True)
    lines = dh.render(drives, {}, {}, 1, {d.name: d.therm["temp"] for d in drives}, True, True)
    check("render", len(lines) > len(drives), True)
    check("good drives beside bad ones", [d.info["status"] for d in drives[:2]], ["ok", "ok"])
    check("boom isolated", (drives[2].info, "parse" in drives[2].err), (None, True))

check("sf_three note", [t for l, t in dh.read_smart("sf_three")[0]["notes"] if l == "warn"],
      ["bad sectors: 3 uncorrectable, 2 offline uncorrectable"])
notes = lambda n: [t for _, t in dh.read_smart(n)[0]["notes"]]
check("sas_bad note", notes("sas_bad"), ["bad sectors: 3 grown defects, 7 uncorrected read errors"])
check("sas_bare says so", any("only the overall verdict" in t for t in notes("sas_bare")), True)
check("verdict_only says so", any("only the overall verdict" in t for t in notes("verdict_only")), True)
check("ds_overlap note", notes("ds_overlap")[0], "bad sectors: 2 reallocated, 7 uncorrectable")

# watch mode: a failed drive scan is shown, and the summary stops saying "all healthy"
dh.USE_COLOR = False
real_find = dh.find_drives
scan = {"fail": False}


def fake_find():
    if scan["fail"]:
        raise dh.subprocess.CalledProcessError(32, "lsblk")
    return [dh.Drive("nv_ok", "?", None, "nv_ok", seq=1)]


real_seq = dh.disk_seq
dh.disk_seq = lambda name: 1
dh.find_drives = fake_find
w = dh.Watcher(True, 1)
w.refresh(); text = "\n".join(w.frame())
check("watch healthy", ("all healthy" in text, "couldn't refresh" in text), (True, False))
scan["fail"] = True
for _ in range(2):
    w.refresh(); text = "\n".join(w.frame())
    check("watch stale", ("all healthy" in text, "couldn't refresh the drive list (lsblk exit status 32" in text,
                          "drive list is stale" in text, len(w.drives)), (False, True, True, 1))
scan["fail"] = False
w.refresh(); text = "\n".join(w.frame())
check("watch recovered", ("all healthy" in text, "couldn't refresh" in text), (True, False))
scan["fail"] = True
w = dh.Watcher(True, 1); w.refresh(); text = "\n".join(w.frame())
check("watch first scan failed", ("Drive inventory unavailable" in text, "last good scan" in text,
                                  "No mounted filesystems" in text), (True, False, False))

# watch mode: same name, same model, no serial, but a new disk sequence number
# is a different drive: fresh peak, no rate from the old drive's counters
state = {"seq": 1, "temp": 70, "io": (1000, 1000, 1000)}
dh.disk_seq = lambda name: state["seq"]
dh.find_drives = lambda: [dh.Drive("sdz", "Same Model", None, "twin", None, state["seq"])]
real_collect, real_stats = dh.collect, dh.diskstats


def fake_collect(drives, have_smart, reuse=()):
    for d in drives:
        d.info, d.err = None, "needs root"
        d.therm = dict(temp=state["temp"], sensors=[], warn=None, crit=None, time_hot=None)


dh.collect, dh.diskstats = fake_collect, lambda: {"sdz": state["io"]}
w = dh.Watcher(False, 1)
w.refresh(); w.frame()
state.update(io=(2_001_000, 1000, 1100)); w.refresh(); w.t0 -= 1
row = [l for l in w.frame() if l.startswith("  twin")]
check("same drive keeps peak and rate", ("70°C" in row[1], "2.0 MB/s" in row[2], "10%" in row[2]), (True, True, True))
state.update(seq=2, temp=30, io=(900_000, 900_000, 1590)); w.refresh(); w.t0 -= 1
row = [l for l in w.frame() if l.startswith("  twin")]
check("replacement: no inherited peak", (row[1].count("30°C"), "70°C" in row[1]), (2, False))
check("replacement: no fabricated rate", ("kB/s" in row[2] or "MB/s" in row[2], "%" in row[2]), (False, False))
state.update(io=(2_900_000, 900_000, 1690)); w.refresh(); w.t0 -= 1
row = [l for l in w.frame() if l.startswith("  twin")]
check("replacement: rates resume next round", "2.0 MB/s" in row[2], True)
check("ident without diskseq", dh.Drive("sda", "M", None, "x", "S1").ident, ("sda", "M", "S1"))
dh.find_drives, dh.collect, dh.diskstats = real_find, real_collect, real_stats
dh.disk_seq = real_seq
dh.subprocess.run = real_run
check("real drives have a disk sequence", all(len(d.ident) == 2 for d in dh.find_drives()), True)
dh.subprocess.run = fake_run

check("rate reset", dh.rates({"sda": (100, 100, 500)}, {"sda": (10, 200, 100)}, 1.0), {})
check("rate normal", dh.rates({"sda": (100, 100, 500)}, {"sda": (1100, 200, 1000)}, 1.0), {"sda": (1000.0, 100.0, 50.0)})
for bad in ("inf", "nan", "0", "-1", "abc", "1e300"):
    try:
        dh.watch_secs(bad)
        check(f"-w {bad} rejected", False, True)
    except dh.argparse.ArgumentTypeError:
        check(f"-w {bad} rejected", True, True)
check("-w 0.1 clamps", dh.watch_secs("0.1"), 0.5)

print(f"{checks - fails}/{checks} checks passed")
sys.exit(1 if fails else 0)
