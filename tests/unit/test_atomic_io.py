import errno

from app.utils import atomic_io


def test_atomic_write_falls_back_for_bind_mounted_file(monkeypatch, tmp_path):
    target = tmp_path / "accounts.json"
    target.write_text("old", encoding="utf-8")

    def busy_replace(_source, _target):
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(atomic_io.os, "replace", busy_replace)
    atomic_io.atomic_write_text(target, '{"accounts": []}')

    assert target.read_text(encoding="utf-8") == '{"accounts": []}'
    assert list(tmp_path.glob("*.tmp")) == []
