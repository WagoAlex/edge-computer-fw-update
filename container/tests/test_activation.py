"""The activation half: staged -> reboot -> confirmed.

`rauc install` writes the INACTIVE slot; the update only goes live on the next
boot, and the bootloader falls back unless the new slot is marked good once it
is running. These tests pin that ordering, because getting it wrong is silent -
mark-good on the wrong slot succeeds and confirms nothing.

The rauc JSON below is verbatim from the edge at 192.168.2.17 on 2026-09-02,
which happened to be sitting in exactly the interesting state: booted rootfs.1,
boot_primary rootfs.2.
"""
import json

import pytest

from providers import firmwareupdate as fw

STAGED = {
    "compatible": "WAGO Edge Computer 752-9xxx", "booted": "/dev/sda2",
    "boot_primary": "rootfs.2",
    "slots": [{"rootfs.1": {"bootname": "A", "state": "booted", "boot_status": "good"}},
              {"rootfs.2": {"bootname": "B", "state": "inactive", "boot_status": "good"}}],
}
REBOOTED = {
    "compatible": "WAGO Edge Computer 752-9xxx", "booted": "/dev/sda3",
    "boot_primary": "rootfs.2",
    "slots": [{"rootfs.1": {"bootname": "A", "state": "inactive", "boot_status": "good"}},
              {"rootfs.2": {"bootname": "B", "state": "booted", "boot_status": "bad"}}],
}
CONFIRMED = json.loads(json.dumps(REBOOTED))
CONFIRMED["slots"][1]["rootfs.2"]["boot_status"] = "good"


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture
def rauc(monkeypatch):
    """Fake `rauc`, recording every invocation. state["status"] is what
    `rauc status --output-format=json` answers next."""
    calls = []
    state = {"status": STAGED, "markgood_rc": 0}

    def fake_run(cmd):
        calls.append(list(cmd))
        if cmd[:2] == ["rauc", "status"] and "--output-format=json" in cmd:
            return _R(0, json.dumps(state["status"]))
        if cmd[:3] == ["rauc", "status", "mark-good"]:
            if state["markgood_rc"]:
                return _R(state["markgood_rc"], "", "no such slot")
            state["status"] = CONFIRMED
            return _R(0, "")
        return _R(0, "")
    monkeypatch.setattr(fw, "run", fake_run)
    fw.rauc_slots.cache_clear()
    yield calls, state
    fw.rauc_slots.cache_clear()


@pytest.fixture(autouse=True)
def clean_state():
    fw.st.update(status=0, progress=0, errorcause=0, debuginfo="", uploads={})
    yield
    fw.st.update(status=0, progress=0, errorcause=0, debuginfo="", uploads={})
    fw.rauc_slots.cache_clear()


# ---- reading the slots -----------------------------------------------------

def test_booted_slot_is_the_one_marked_booted_not_the_device_path(rauc):
    # rauc's own "booted" key is /dev/sda2 - a device, not a slot name.
    assert fw.rauc_slots() == ("rootfs.1", "rootfs.2", True)


def test_pending_is_empty_once_the_staged_slot_is_the_booted_one(rauc):
    calls, state = rauc
    state["status"] = REBOOTED
    fw.rauc_slots.cache_clear()
    booted, pending, good = fw.rauc_slots()
    assert (booted, pending, good) == ("rootfs.2", "", False)


def test_no_rauc_on_this_host_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(fw, "run", lambda cmd: _R(127, "", "rauc: not found"))
    fw.rauc_slots.cache_clear()
    assert fw.rauc_slots() == ("", "", True)
    assert fw.activation_state() == 9            # NotAvailable, never a crash


# ---- the readable parameter ------------------------------------------------

def test_activationstate_is_unconfirmed_while_a_slot_is_staged(rauc):
    assert fw.activation_state() == 4


def test_activationstate_is_unconfirmed_after_reboot_until_marked_good(rauc):
    calls, state = rauc
    state["status"] = REBOOTED
    fw.rauc_slots.cache_clear()
    assert fw.activation_state() == 4            # booted, but boot_status bad


def test_activationstate_is_confirmed_once_the_booted_slot_is_good(rauc):
    calls, state = rauc
    state["status"] = CONFIRMED
    fw.rauc_slots.cache_clear()
    assert fw.activation_state() == 5


def test_activationstate_survives_a_process_restart(rauc):
    """The reboot that activates a slot restarts this container, so nothing may
    depend on `st` - a fresh process must still report Unconfirmed."""
    fw.st["status"] = 0                          # as if just started
    assert fw.activation_state() == 4


# ---- finish no longer confirms ---------------------------------------------

def test_finish_does_not_mark_good(rauc):
    calls, _state = rauc
    fw.st["status"] = 4
    out, dsc, detail = fw.m_finish({})
    assert dsc is None and fw.st["status"] == 8
    assert not any("mark-good" in c for call in calls for c in call), \
        "finish runs on the OLD slot; mark-good there confirms the wrong one"


def test_finish_still_refuses_outside_unconfirmed(rauc):
    fw.st["status"] = 2
    _out, dsc, _detail = fw.m_finish({})
    assert dsc is not None


# ---- confirm ---------------------------------------------------------------

def test_confirm_is_refused_while_a_slot_is_still_pending(rauc):
    calls, _state = rauc
    out, dsc, detail = fw.m_confirm({})
    assert out is None and dsc == "1"
    assert "reboot" in detail
    assert not any("mark-good" in c for call in calls for c in call)


def test_confirm_marks_the_booted_slot_good_after_the_reboot(rauc):
    calls, state = rauc
    state["status"] = REBOOTED
    fw.rauc_slots.cache_clear()
    out, dsc, _detail = fw.m_confirm({})
    assert dsc is None
    assert ["rauc", "status", "mark-good", "booted"] in calls
    assert out["Slot"]["value"] == "rootfs.2"
    assert fw.st["status"] == 5                  # Confirmed
    assert fw.activation_state() == 5


def test_confirm_reports_a_rauc_failure_instead_of_claiming_success(rauc):
    calls, state = rauc
    state["status"], state["markgood_rc"] = REBOOTED, 1
    fw.rauc_slots.cache_clear()
    out, dsc, detail = fw.m_confirm({})
    assert out is None and dsc == "1" and "no such slot" in detail
    assert fw.st["status"] != 5


def test_confirm_without_rauc_says_so(monkeypatch):
    monkeypatch.setattr(fw, "run", lambda cmd: _R(127, "", "rauc: not found"))
    fw.rauc_slots.cache_clear()
    out, dsc, detail = fw.m_confirm({})
    assert out is None and "no booted slot" in detail


# ---- reboot is opt-in ------------------------------------------------------

def test_reboot_needs_an_explicit_confirm_flag(monkeypatch):
    called = []
    monkeypatch.setattr(fw.hostcfg, "reboot", lambda: called.append(1) or (True, ""))
    for inargs in ({}, {"Confirm": {"value": False}}, {"Confirm": {"value": "true"}}):
        out, dsc, detail = fw.m_reboot(inargs)
        assert out is None and "never implicit" in detail
    assert called == []


def test_reboot_with_confirm_calls_logind(monkeypatch):
    called = []
    monkeypatch.setattr(fw.hostcfg, "reboot", lambda: called.append(1) or (True, ""))
    _out, dsc, _detail = fw.m_reboot({"Confirm": {"value": True}})
    assert dsc is None and called == [1]


def test_reboot_reports_a_refusal(monkeypatch):
    monkeypatch.setattr(fw.hostcfg, "reboot",
                        lambda: (False, "Interactive authentication required"))
    out, dsc, detail = fw.m_reboot({"Confirm": {"value": True}})
    assert out is None and "Interactive authentication" in detail


def test_no_other_method_ever_reboots(rauc, monkeypatch):
    """The whole point of the opt-in: replaying the update sequence must not
    restart the device."""
    called = []
    monkeypatch.setattr(fw.hostcfg, "reboot", lambda: called.append(1) or (True, ""))
    fw.st["status"] = 4
    fw.m_finish({})
    state = rauc[1]
    state["status"] = REBOOTED
    fw.rauc_slots.cache_clear()
    fw.m_confirm({})
    fw.m_clear({})
    assert called == []
