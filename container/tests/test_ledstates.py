"""0-0-ledstates: one honest LED, sourced from sysfs when the platform has one."""
import providers
from providers import ledstates


def test_single_instantiation():
    assert providers.param_value("0-0-ledstates") == [{"Classes": ["LED"], "Id": 1}]
    assert providers.param_value("0-0-ledstates-1-name") == "RUN"
    # ids WAGO's model has but this device does not: absent, not faked green.
    assert providers.param_value("0-0-ledstates-2-name") is None


def test_sysfs_wins_over_systemd(tmp_path, monkeypatch):
    node = tmp_path / "platform::power"
    node.mkdir()
    (node / "brightness").write_text("1\n")
    monkeypatch.setattr(ledstates, "PWR_SYSFS", str(node))
    monkeypatch.setattr(ledstates.hostcfg, "systemd_state", lambda: "degraded")
    ledstates._state.cache_clear()
    assert providers.param_value("0-0-ledstates-1-colors") == [ledstates.GREEN]
    assert "PWR LED lit" in providers.param_value("0-0-ledstates-1-diagnosticinformation")

    (node / "brightness").write_text("0\n")
    ledstates._state.cache_clear()
    assert providers.param_value("0-0-ledstates-1-colors") == [ledstates.OFF]


def test_falls_back_to_systemd_state(monkeypatch):
    monkeypatch.setattr(ledstates, "PWR_SYSFS", "")
    monkeypatch.setattr(ledstates, "SYSFS", "/nonexistent")
    monkeypatch.setattr(ledstates.hostcfg, "systemd_state", lambda: "degraded")
    ledstates._state.cache_clear()
    assert providers.param_value("0-0-ledstates-1-colors") == [ledstates.GREEN, ledstates.OFF]
    assert "degraded" in providers.param_value("0-0-ledstates-1-diagnosticinformation")


def test_unknown_state_does_not_claim_green(monkeypatch):
    monkeypatch.setattr(ledstates, "PWR_SYSFS", "")
    monkeypatch.setattr(ledstates, "SYSFS", "/nonexistent")
    monkeypatch.setattr(ledstates.hostcfg, "systemd_state", lambda: None)
    ledstates._state.cache_clear()
    assert providers.param_value("0-0-ledstates-1-colors") == [ledstates.GREEN, ledstates.OFF]
    assert "unavailable" in providers.param_value("0-0-ledstates-1-diagnosticinformation")


def test_colour_enum_matches_wago(monkeypatch):
    # Read off a real PFC300: /wda/parameter-definitions/0-0-ledstates-1-colors/enum
    assert providers.ENUMS["0-0-ledstates-1-colors"][1] == "LED_COLOR_GREEN"
    assert providers.ENUMS["0-0-ledstates-1-colors"][7] == "LED_COLOR_OFF"
