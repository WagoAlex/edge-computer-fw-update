"""Phase 0 guard: the firmware-update WDA surface must survive the refactor."""
import providers
from providers import firmwareupdate as fw


def test_firmwareupdate_methods_unchanged():
    assert set(m for m in providers.METHODS if m.startswith("0-0-firmwareupdate-")) == {
        "0-0-firmwareupdate-activate", "0-0-firmwareupdate-getuploadids",
        "0-0-firmwareupdate-start", "0-0-firmwareupdate-finish",
        "0-0-firmwareupdate-clear", "0-0-firmwareupdate-cancel",
        "0-0-firmwareupdate-settimeout", "0-0-firmwareupdate-getlastlogentries"}


def test_firmwareupdate_params_unchanged():
    for pid in ("status", "progress", "errorcause", "debuginfo", "revertable"):
        assert providers.param_value(f"0-0-firmwareupdate-{pid}") is not None


def test_enums_not_renumbered():
    # fw_update.py branches on these exact names/numbers.
    assert providers.ENUMS["0-0-firmwareupdate-status"][4] == "Unconfirmed"
    assert providers.ENUMS["0-0-firmwareupdate-errorcause"][602] == "SignatureTooOld"
    assert (fw.DSC_NOT_ACTIVATED, fw.DSC_ALREADY_ACTIVE) == ("95", "90")


def test_unknown_parameter_is_none():
    assert providers.param_value("0-0-nosuch-thing") is None


def test_state_machine_gating(tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "STAGE_DIR", str(tmp_path))
    fw.m_clear({})
    # start before activate -> "not activated" (95), the code fw_update.py checks
    assert fw.m_start({})[1] == fw.DSC_NOT_ACTIVATED
    assert fw.m_activate({})[1] is None
    assert fw.m_activate({})[1] == fw.DSC_ALREADY_ACTIVE   # 90, already active
    out, err, _ = fw.m_getuploadids({"FileNames": {"value": ["fw.raucb"]}})
    assert err is None and len(out["UploadFiles"]["value"]) == 1
    fid = out["UploadFiles"]["value"][0]
    assert fw.upload(fid)["name"] == "fw.raucb"
    fw.m_clear({})
    assert providers.param_value("0-0-firmwareupdate-status") == 0


def test_install_completion_does_not_deadlock(tmp_path, monkeypatch):
    """_install_worker calls logline() while holding _lock. With a non-reentrant
    lock that wedges the whole API at the end of every real install - the reads
    after it never return. Regression: seen live on the edge 2026-09-01."""
    import subprocess
    import threading

    class FakeProc:
        returncode = 0
        stdout = iter(["  10% installing\n", " 100% done\n"])

        def wait(self):
            return 0

    monkeypatch.setattr(fw.subprocess, "Popen", lambda *a, **k: FakeProc())
    src = tmp_path / "b.raucb"
    src.write_bytes(b"x")

    worker = threading.Thread(target=fw._install_worker, args=(str(tmp_path / "dst.raucb"),),
                              kwargs={"stage_from": str(src)}, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "install worker deadlocked holding _lock"

    # and the API must still answer afterwards
    reader = threading.Thread(
        target=lambda: providers.param_value("0-0-firmwareupdate-status"), daemon=True)
    reader.start()
    reader.join(timeout=5)
    assert not reader.is_alive(), "parameter read blocked on a lock held by the worker"
    assert providers.param_value("0-0-firmwareupdate-status") == 4      # Unconfirmed
    assert providers.param_value("0-0-firmwareupdate-progress") == 100
    fw.m_clear({})
