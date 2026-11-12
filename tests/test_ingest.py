from izmir_mcp.ingest import _first_list, parse_eshot_lines, parse_eshot_stops


def test_parse_stops_variants():
    rows = parse_eshot_stops({"kayitlar": [
        {"DURAK_ID": 30005, "DURAK_ADI": "Bornova Metro", "ENLEM": "38,4585", "BOYLAM": "27,2138",
         "DURAKTAN_GECEN_HATLAR": "267-268-585"},
        {"DurakId": "40010", "DurakAdi": "Şirinyer", "Enlem": 38.39, "Boylam": 27.15},
        {"foo": "bar"},
    ]})
    assert [s.id for s in rows] == ["30005", "40010"]
    assert rows[0].lat == 38.4585 and rows[0].lines == ["267", "268", "585"]


def test_parse_lines():
    rows = parse_eshot_lines([{"HAT_NO": 505, "HAT_ADI": "ÇAMKULE - BORNOVA METRO"}])
    assert rows[0].number == "505"


def test_first_list():
    assert _first_list([{"a": 1}]) == [{"a": 1}]
    assert _first_list({"x": 1}) == []


def test_bad_database_url_does_not_leak_password(monkeypatch, capsys):
    import pytest

    from izmir_mcp.config import get_settings
    from izmir_mcp.ingest import main

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "database_url", "GizliSifre123.")
    with pytest.raises(SystemExit) as exc:
        main(["--only", "districts"])
    assert "postgresql://" in str(exc.value)
    assert "GizliSifre123" not in str(exc.value)
    get_settings.cache_clear()


def test_parse_real_eshot_stop_format():
    """2026-11-11'de DuraklariGetir'den gelen gerçek biçim."""
    rows = parse_eshot_stops([
        {"Id": 6835.0, "DurakAdi": "Dilek", "DurakKodu": 41154.0, "X_Koordinati": "38,37000878299070",
         "y_koordinati": "27,17355753713920", "IlceAdi": "BUCA", "AcikKapali": 1},
        {"Id": 3746.0, "DurakAdi": "Düverlik Meydan", "DurakKodu": 13885.0, "X_Koordinati": "38,218004",
         "y_koordinati": "27,449238", "IlceAdi": "TORBALI", "AcikKapali": 2},
    ])
    assert len(rows) == 1  # kapalı durak atlandı
    s = rows[0]
    assert s.id == "41154"  # Id değil DurakKodu, ".0" olmadan
    assert round(s.lat, 4) == 38.37 and round(s.lon, 4) == 27.1736
    assert s.district_name == "BUCA"


def test_parse_real_eshot_line_format():
    rows = parse_eshot_lines([{"HatNo": "5", "HatId": 5.0, "HatAdi": "NARLIDERE - ÜÇKUYULAR İSKELE",
                               "HatBaslangic": "NARLIDERE", "HatBitis": "ÜÇKUYULAR İSKELE"}])
    assert rows[0].number == "5" and rows[0].start == "NARLIDERE" and rows[0].end == "ÜÇKUYULAR İSKELE"
