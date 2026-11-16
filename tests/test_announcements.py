from datetime import UTC, datetime

import httpx
import pytest

from izmir_mcp import http, tools
from izmir_mcp.models import ErrorCode
from izmir_mcp.sources import announcements

from .conftest import FIX, load

CSV = "/211488/docs/eshot-otobus-hat-duyurulari.csv"
LINES = "/AcikVeriEshot/Hatlar/HatlariGetir"


def _at(monkeypatch, *args):
    monkeypatch.setattr(tools, "_now", lambda: datetime(*args, tzinfo=UTC))


@pytest.fixture
def ann(fake, monkeypatch):
    fake.routes[CSV] = (200, (FIX / "hat_duyurulari.csv").read_text("utf-8"))  # 120 satır, 37 farklı duyuru
    fake.routes[LINES] = (200, load("eshot_hatlar.json"))  # 10 hatlık örnek
    _at(monkeypatch, 2026, 11, 15, 14, 0)  # İstanbul 17:00
    return fake


def test_line_code():
    assert announcements.line_code("302") == "302"
    assert announcements.line_code("302 nolu hat") == "302"
    assert announcements.line_code("Hat 034") == "34"
    assert announcements.line_code("otobüs") is None


def test_decode_text_handles_bom_and_old_turkish_encoding():
    assert http._decode_text("﻿HAT_NO;BASLIK".encode()) == "HAT_NO;BASLIK"
    assert http._decode_text("ŞİRİNYER".encode("cp1254")) == "ŞİRİNYER"


async def test_all_lines_grouped_active_first(ann):
    r = await tools.get_announcements(limit=50)
    assert r.error is None and r.line is None
    assert r.total == 37  # aynı duyurunun hat başına satırları tek kayıtta birleşti
    statuses = [a.status for a in r.announcements]
    assert statuses.count("upcoming") == 1 and statuses[-1] == "upcoming"
    assert r.line_notes == []
    assert all(c != LINES for c in ann.calls)  # hat verilmeyince hat listesi indirilmez


async def test_limit_and_upcoming_filter(ann):
    r = await tools.get_announcements(limit=5)
    assert len(r.announcements) == 5 and r.total == 37
    assert any("37 duyurudan ilk 5" in n for n in r.meta.notes)
    r = await tools.get_announcements(include_upcoming=False, limit=50)
    assert r.total == 36 and all(a.status == "active" for a in r.announcements)


async def test_single_line_with_shared_announcement(ann):
    r = await tools.get_announcements("34 nolu hat")
    assert r.error is None and r.line == "34" and r.line_name == "ESENTEPE - GÜMRÜK"
    assert len(r.announcements) == 1
    a = r.announcements[0]
    assert a.status == "active" and "GEÇİCİ GÜZERGAH" in a.title
    assert a.lines == ["34", "39", "41", "42", "44", "45", "46", "234"]
    assert a.starts_at.isoformat() == "2026-10-30T14:40:00+03:00"


async def test_line_missing_from_sample_still_gets_csv_announcements(ann):
    r = await tools.get_announcements("41")
    assert r.error is None and len(r.announcements) == 2 and r.line_name is None


async def test_permanent_line_notes_are_deduplicated(ann):
    r = await tools.get_announcements("18")
    assert r.announcements == []
    assert [n.note for n in r.line_notes] == ["HATTIMIZ RİNG HAT OLUP KONAKTAN SAAT UYGULAMASI YOKTUR"]
    assert any("geçerli bir duyuru yok" in n for n in r.meta.notes)


async def test_expired_announcement_is_hidden(ann, monkeypatch):
    assert len((await tools.get_announcements("989")).announcements) == 1
    _at(monkeypatch, 2026, 11, 21, 9, 0)  # 20 Kasım'da bitti
    r = await tools.get_announcements("989")
    assert r.error is None and r.announcements == [] and r.line_name == "ORHANLI - SEFERİHİSAR"


async def test_upcoming_announcement(ann):
    r = await tools.get_announcements("776")
    assert [a.status for a in r.announcements] == ["upcoming"]
    assert r.announcements[0].lines == ["776", "778"]
    r = await tools.get_announcements("776", include_upcoming=False)
    assert r.announcements == []


async def test_unknown_and_invalid_line(ann):
    r = await tools.get_announcements("9999")
    assert r.error.code == ErrorCode.NOT_FOUND
    calls = len(ann.calls)
    r = await tools.get_announcements("otobüs")
    assert r.error.code == ErrorCode.INVALID_INPUT and len(ann.calls) == calls


async def test_csv_down_keeps_line_notes(ann):
    ann.routes[CSV] = httpx.ConnectError("down")
    r = await tools.get_announcements("18")
    assert r.error is None and r.line_notes
    assert any("duyuruları dosyası şu an alınamıyor" in n for n in r.meta.notes)
    r = await tools.get_announcements()
    assert r.error is not None and r.error.code == ErrorCode.SOURCE_UNAVAILABLE


async def test_line_list_down_keeps_announcements(ann):
    ann.routes[LINES] = httpx.ConnectError("down")
    r = await tools.get_announcements("34")
    assert r.error is None and len(r.announcements) == 1 and r.line_notes == []
    assert any("hat bilgileri şu an alınamıyor" in n for n in r.meta.notes)


async def test_source_status_uses_light_head_requests_for_big_files(ann):
    status = await tools.get_source_status()
    by = {x.source: x for x in status.sources}
    assert by[announcements.SOURCE_CSV].state == "up" and by[announcements.SOURCE_LINES].state == "up"
    big = [(m, p) for m, p in ann.methods if p in (CSV, LINES)]
    assert big and all(m == "HEAD" for m, _ in big)  # içerik indirilmedi


async def test_head_probe_status_codes(ann):
    ann.routes[LINES] = (405, {})  # HEAD desteklenmiyor ama sunucu ayakta
    ann.routes[CSV] = (404, {})  # dosya kaldırılmış: gerçek bir sorun
    status = await tools.get_source_status()
    by = {x.source: x for x in status.sources}
    assert by[announcements.SOURCE_LINES].state == "up"
    assert by[announcements.SOURCE_CSV].state == "degraded"
