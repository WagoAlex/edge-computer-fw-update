"""system provider: A/B slots from rauc, identity/version, memory card."""
import importlib
import json
import subprocess

import providers.system as system

RAUC_JSON = json.dumps({"compatible": "WAGO Edge Computer 752-9xxx", "slots": [
    {"rootfs.1": {"state": "booted", "boot_status": "good", "device": "/dev/sda2",
                  "mountpoint": "/"}},
    {"rootfs.2": {"state": "inactive", "boot_status": "bad", "device": "/dev/sda3"}}]})


def fake_rauc(monkeypatch, stdout=RAUC_JSON, exc=None):
    def run(cmd, **kw):
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, 0, stdout, "")
    monkeypatch.setattr(system.subprocess, "run", run)
    system._rauc_status.cache_clear()


def test_slots_project_onto_wago_systems(monkeypatch):
    fake_rauc(monkeypatch)
    assert system.PARAMS["0-0-systems-1-active"]() is True       # booted slot
    assert system.PARAMS["0-0-systems-1-configured"]() is True   # marked good
    assert system.PARAMS["0-0-systems-2-active"]() is False
    assert system.PARAMS["0-0-systems-2-configured"]() is False  # boot_status bad
    assert system.PARAMS["0-0-systems-2-available"]() is True    # slot exists
    assert system.PARAMS["0-0-systems"]() == [
        {"Classes": ["SystemRecoverySystem"], "Id": 1},
        {"Classes": ["SystemRecoverySystem"], "Id": 2}]


def test_rauc_unavailable_reports_false_not_crash(monkeypatch):
    fake_rauc(monkeypatch, exc=FileNotFoundError("rauc"))
    assert system.PARAMS["0-0-systems-1-active"]() is False


def test_rauc_garbage_output_reports_false_not_crash(monkeypatch):
    fake_rauc(monkeypatch, stdout="not json")
    assert system.PARAMS["0-0-systems-1-configured"]() is False


def test_identity_prefers_env_then_dmi(tmp_path, monkeypatch):
    dmi = tmp_path / "dmi"
    dmi.mkdir()
    (dmi / "product_serial").write_text("37SUN31564010260389679\n")
    monkeypatch.setenv("DMI_DIR", str(dmi))
    monkeypatch.setenv("ORDER_NUMBER", "0752-9401")
    monkeypatch.delenv("SERIAL_NUMBER", raising=False)
    importlib.reload(system)
    assert system.PARAMS["0-0-identity-ordernumber"]() == "0752-9401"
    assert system.PARAMS["0-0-identity-serialnumber"]() == "37SUN31564010260389679"
    monkeypatch.setenv("SERIAL_NUMBER", "override")
    assert system.PARAMS["0-0-identity-serialnumber"]() == "override"


def test_memorycard_absent_and_present(tmp_path, monkeypatch):
    blocks = tmp_path / "block"
    blocks.mkdir()
    monkeypatch.setenv("SYS_BLOCK", str(blocks))
    importlib.reload(system)
    assert system.PARAMS["0-0-memorycard-isavailable"]() is False
    (blocks / "mmcblk0").mkdir()
    (blocks / "mmcblk0" / "ro").write_text("1")
    system._memorycard.cache_clear()
    assert system.PARAMS["0-0-memorycard-isavailable"]() is True
    assert system.PARAMS["0-0-memorycard-iswriteprotected"]() is True
    assert system.PARAMS["0-0-memorycard-volumename"]() == "mmcblk0"
