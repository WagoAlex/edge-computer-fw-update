"""0-0-presets-* WDA methods over the store."""
import importlib

import pytest

import presets
import providers.preset as pp


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PRESET_DIR", str(tmp_path / "pre"))
    monkeypatch.setenv("CUSTOM_PRESET_DIR", str(tmp_path / "custom"))
    (tmp_path / "pre").mkdir()
    importlib.reload(presets)
    importlib.reload(pp)
    return pp


def args(**kw):
    return {k: {"value": v} for k, v in kw.items()}


def test_save_get_list_delete_round_trip(store):
    out, err, _ = store.METHODS["0-0-presets-save"](args(
        Name="site-a", Description="X1 static + our DNS",
        Parameters={"0-0-networking-bridges-1-ipconfiguration-addresses": ["192.168.2.17/24"],
                    "0-0-networking-dns-customdnsservers": ["192.168.2.1"]}))
    assert err is None and out["Preset"]["value"]["predefined"] is False

    out, err, _ = store.METHODS["0-0-presets-get"](args(Name="site-a"))
    assert err is None
    assert out["Preset"]["value"]["parameters"]["0-0-networking-dns-customdnsservers"] \
        == ["192.168.2.1"]

    out, _, _ = store.METHODS["0-0-presets-list"]({})
    assert [p["name"] for p in out["Presets"]["value"]] == ["site-a"]

    out, err, _ = store.METHODS["0-0-presets-delete"](args(Name="site-a"))
    assert err is None and out["Deleted"]["value"] is True
    assert store.METHODS["0-0-presets-get"](args(Name="site-a"))[1] is not None


def test_missing_preset_is_a_wda_error_not_a_crash(store):
    _, err, detail = store.METHODS["0-0-presets-get"](args(Name="nope"))
    assert err is not None and "no such preset" in detail


def test_path_traversal_rejected_through_the_method(store):
    _, err, detail = store.METHODS["0-0-presets-save"](args(
        Name="../../etc/cron.d/x", Description="", Parameters={}))
    assert err is not None and "invalid preset name" in detail


def test_non_wda_parameter_ids_rejected(store):
    """A preset is a WDA-parameter fragment - nothing else can ever be applied."""
    _, err, detail = store.METHODS["0-0-presets-save"](args(
        Name="junk", Description="", Parameters={"rm": "-rf"}))
    assert err is not None and "not WDA parameter ids" in detail


def test_apply_is_an_explicit_error_not_a_silent_success(store):
    out, err, detail = store.METHODS["0-0-presets-apply"](args(Name="site-a"))
    assert out is None and err is not None and "Phase 3" in detail
