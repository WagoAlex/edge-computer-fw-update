"""Preset store: predefined in the image, custom on the data volume."""
import importlib
import json

import pytest

import presets


@pytest.fixture
def store(tmp_path, monkeypatch):
    pre, custom = tmp_path / "pre", tmp_path / "custom"
    pre.mkdir()
    (pre / "dns-quad9.json").write_text(json.dumps(
        {"description": "quad9", "parameters": {"0-0-networking-dns-customdnsservers": ["9.9.9.9"]}}))
    monkeypatch.setenv("PRESET_DIR", str(pre))
    monkeypatch.setenv("CUSTOM_PRESET_DIR", str(custom))
    importlib.reload(presets)
    return presets


def test_predefined_are_listed_and_flagged(store):
    [p] = store.list_presets()
    assert p["name"] == "dns-quad9" and p["predefined"] is True


def test_custom_preset_round_trips_across_restart(store, tmp_path, monkeypatch):
    store.save("site-dns", "our resolvers", {"0-0-networking-dns-customdnsservers": ["192.168.2.1"]})
    importlib.reload(presets)                       # simulate a container restart
    monkeypatch.setenv("PRESET_DIR", str(tmp_path / "pre"))
    monkeypatch.setenv("CUSTOM_PRESET_DIR", str(tmp_path / "custom"))
    importlib.reload(presets)
    p = presets.get("site-dns")
    assert p["parameters"] == {"0-0-networking-dns-customdnsservers": ["192.168.2.1"]}
    assert p["predefined"] is False


def test_custom_shadows_predefined(store):
    store.save("dns-quad9", "mine", {"0-0-networking-dns-customdnsservers": ["1.1.1.1"]})
    assert store.get("dns-quad9")["parameters"]["0-0-networking-dns-customdnsservers"] \
        == ["1.1.1.1"]
    assert len(store.list_presets()) == 1           # one name, not two


def test_delete_only_removes_custom(store):
    store.save("dns-quad9", "mine", {"0-0-networking-dns-customdnsservers": ["1.1.1.1"]})
    assert store.delete("dns-quad9") is True
    assert store.delete("dns-quad9") is False       # predefined stays in the image
    assert store.get("dns-quad9")["predefined"] is True


@pytest.mark.parametrize("name", ["../escape", "a/b", "", "x" * 65, ".hidden", "no space"])
def test_path_traversal_and_junk_names_rejected(store, name):
    with pytest.raises(store.PresetError):
        store.save(name, "", {})
    with pytest.raises(store.PresetError):
        store.get(name)


def test_non_object_parameters_rejected(store):
    with pytest.raises(store.PresetError):
        store.save("bad", "", ["not", "a", "dict"])


def test_corrupt_file_does_not_blank_the_list(store, tmp_path):
    (tmp_path / "custom").mkdir(exist_ok=True)
    (tmp_path / "custom" / "broken.json").write_text("{not json")
    assert [p["name"] for p in store.list_presets()] == ["dns-quad9"]


def test_shipped_presets_are_wellformed():
    """The JSON that actually ships in the image must load."""
    importlib.reload(presets)
    for p in presets.list_presets():
        assert p["predefined"] and isinstance(p["parameters"], dict)
        for pid in p["parameters"]:
            assert pid.startswith("0-0-"), pid
