"""localusers: instance id is the uid (root is 1), shadow is optional."""
import importlib

import providers.localusers as lu

PASSWD = ("root:x:0:0:root:/root:/bin/bash\n"
          "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
          "www:x:13:13:www:/var/www:/bin/sh\n"
          "user:x:1001:1001::/home/user:/bin/bash\n"
          "sync:x:4:65534:sync:/bin:/bin/sync\n"
          "nobody:x:65534:65534::/nonexistent:/usr/sbin/nologin\n")


def setup(tmp_path, monkeypatch, shadow=None):
    p = tmp_path / "passwd"
    p.write_text(PASSWD)
    monkeypatch.setenv("PASSWD_FILE", str(p))
    if shadow is None:
        monkeypatch.setenv("SHADOW_FILE", str(tmp_path / "absent"))
    else:
        s = tmp_path / "shadow"
        s.write_text(shadow)
        monkeypatch.setenv("SHADOW_FILE", str(s))
    importlib.reload(lu)
    return lu


def test_nologin_and_sync_accounts_are_skipped(tmp_path, monkeypatch):
    """nologin/false/sync are not login accounts - the cassette lists none."""
    m = setup(tmp_path, monkeypatch)
    assert m.PARAMS["0-0-localusers"]() == [{"Classes": ["LocalUser"], "Id": i}
                                            for i in (1, 13, 1001)]


def test_root_is_instance_1_like_the_cassette(tmp_path, monkeypatch):
    m = setup(tmp_path, monkeypatch)
    assert m.RESOLVE("0-0-localusers-1-name") == "root"
    assert m.RESOLVE("0-0-localusers-1001-name") == "user"
    assert m.RESOLVE("0-0-localusers-4242-name") is m.NOTFOUND


def test_without_shadow_expiry_is_false_not_a_guess(tmp_path, monkeypatch):
    m = setup(tmp_path, monkeypatch)
    assert m.RESOLVE("0-0-localusers-13-ispasswordexpired") is False


def test_with_shadow_zero_lastchange_means_expired(tmp_path, monkeypatch):
    m = setup(tmp_path, monkeypatch, shadow="root:!:19000:0:99999:7:::\nwww:x:0:0:99999:7:::\n")
    assert m.RESOLVE("0-0-localusers-1-ispasswordexpired") is False
    assert m.RESOLVE("0-0-localusers-13-ispasswordexpired") is True


def test_past_max_age_means_expired(tmp_path, monkeypatch):
    m = setup(tmp_path, monkeypatch, shadow="user:x:10000:0:30:7:::\n")
    assert m.RESOLVE("0-0-localusers-1001-ispasswordexpired") is True
