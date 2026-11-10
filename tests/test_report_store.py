from dataclasses import dataclass

from src.services import report_store


@dataclass
class Box:
    value: int


def test_finished_report_is_reused_until_it_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(report_store, "DATA_DIR", tmp_path)
    key = ("token", "2026-10-11", "2026-11-09", ((1, (11,), "AUTO"),))
    report_store.save_finished("rsya", key, [Box(7)], finished_at=report_store.time.time())
    finished_at, cabinets = report_store.load_finished("rsya", key)
    assert cabinets[0].value == 7
    assert finished_at > 0

    report_store.save_finished("rsya", key, [Box(1)], finished_at=report_store.time.time() - report_store.RESULT_TTL_SECONDS - 5)
    assert report_store.load_finished("rsya", key) is None


def test_partial_cabinets_are_kept_and_cleared_by_finished_report(tmp_path, monkeypatch):
    monkeypatch.setattr(report_store, "DATA_DIR", tmp_path)
    key = ("token", "2026-10-11", "2026-11-09")
    report_store.remember_partial("keywords", key, 4, Box(4))
    report_store.remember_partial("keywords", key, 9, Box(9))
    assert set(report_store.load_partial("keywords", key)) == {4, 9}

    report_store.save_finished("keywords", key, [Box(4), Box(9)], finished_at=report_store.time.time())
    assert report_store.load_partial("keywords", key) == {}
    assert len(report_store.load_finished("keywords", key)[1]) == 2


def test_clear_removes_saved_report(tmp_path, monkeypatch):
    monkeypatch.setattr(report_store, "DATA_DIR", tmp_path)
    key = ("token",)
    report_store.save_finished("texts", key, [Box(1)], finished_at=report_store.time.time())
    report_store.clear("texts", key)
    assert report_store.load_finished("texts", key) is None
