"""Conformance with WAGO's own OpenAPI 3.1 document (WDA 1.5.2).

The reference shape below was captured from a live CC100 at 192.168.42.110 on
2026-09-01 - `GET /wda/parameters/0-0-firmwareupdate-status` - not invented here.
"""
import json

import openapi
import providers
from providers import meta

# Verbatim from the real device.
REAL_DEVICE_RESPONSE = {
    "data": {
        "attributes": {"dataRank": "scalar", "dataType": "enum_member",
                       "path": "FirmwareUpdate/Status", "value": 0},
        "id": "0-0-firmwareupdate-status",
        "links": {"self": "/wda/parameters/0-0-firmwareupdate-status"},
        "relationships": {
            "definition": {"links": {"related": "/wda/parameters/0-0-firmwareupdate-status/definition"}},
            "device": {"links": {"related": "/wda/parameters/0-0-firmwareupdate-status/device"}}},
        "type": "parameters"},
    "jsonapi": {"version": "1.0"},
    "links": {"self": "/wda/parameters/0-0-firmwareupdate-status"},
    "meta": {"doc": "/openapi/wda.openapi.html#operation/getParameter",
             "version": "1.5.2"},
}


def test_parameter_metadata_matches_the_real_device():
    d = meta.describe("0-0-firmwareupdate-status", 0)
    real = REAL_DEVICE_RESPONSE["data"]["attributes"]
    assert d["dataType"] == real["dataType"]
    assert d["dataRank"] == real["dataRank"]
    assert d["path"] == real["path"]


def test_every_served_parameter_has_real_metadata():
    """No served parameter may fall through to inferred type with an empty path -
    that would mean we are serving an id the cassette does not know."""
    missing = []
    for pid in providers.PARAMS:
        d = meta.describe(pid, providers.param_value(pid))
        if not d["path"]:
            missing.append(pid)
    assert missing == [], f"no cassette metadata for: {missing}"


def test_instance_ids_get_their_number_in_the_path():
    d = meta.describe("0-0-networking-ethernetports-11-name", "X11")
    assert d["path"] == "Networking/EthernetPorts/11/Name"
    assert d["dataType"] == "string"


def test_unknown_id_infers_type_and_admits_it_has_no_path():
    d = meta.describe("0-0-nosuch-thing", ["a", "b"])
    assert (d["dataType"], d["dataRank"], d["path"]) == ("string", "array", "")


def test_spec_is_valid_json_and_declares_basic_auth_only():
    d = openapi.document("0752-9xxx", "04.01.00", "1.5.2-compat")
    json.dumps(d)                                    # must be serialisable
    assert d["openapi"] == "3.1.0"
    assert d["security"] == [{"password_based": []}]
    assert d["components"]["securitySchemes"]["password_based"] == {
        "type": "http", "scheme": "basic"}


def test_spec_is_generated_from_the_registry_not_hand_written():
    d = openapi.document("0752-9xxx", "04.01.00", "1.5.2-compat")
    ids = d["paths"]["/wda/parameters/{parameter_id}"]["parameters"][0]["schema"]["enum"]
    methods = d["paths"]["/wda/methods/{method_id}/runs"]["parameters"][0]["schema"]["enum"]
    assert set(ids) == set(providers.PARAMS)
    assert set(methods) == set(providers.METHODS)


def test_spec_admits_it_is_a_subset():
    d = openapi.document("0752-9xxx", "04.01.00", "1.5.2-compat")
    desc = d["info"]["description"]
    assert "STRICT SUBSET" in desc
    assert "self-signed" in desc          # must not imply real-WDA acceptance
    assert "/wda/parameters GET" in desc  # names the discovery paths we lack
    # PATCH exists now, but only for the ids the registry actually accepts
    assert "x-writable-parameters" in desc
    assert d["x-writable-parameters"] == providers.writable_ids()


def test_health_is_documented_as_unauthenticated():
    d = openapi.document("0752-9xxx", "04.01.00", "1.5.2-compat")
    assert d["paths"]["/health"]["get"]["security"] == []
