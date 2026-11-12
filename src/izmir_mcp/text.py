import re
import unicodedata

_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})
_ASCII = str.maketrans({"ı": "i", "ğ": "g", "ü": "u", "ş": "s", "ö": "o", "ç": "c", "â": "a", "î": "i", "û": "u"})
_STOP_WORDS = {"durak", "duragi", "duraği", "durağı", "duraginda", "durağında", "otobus", "otobüs", "istasyonu"}


def norm(s: str | None) -> str:
    """Türkçe büyük/küçük harf ve aksan farkını yok sayan arama anahtarı: 'BUCA 1' -> 'buca 1'."""
    if not s:
        return ""
    s = s.translate(_TR_LOWER).lower().translate(_ASCII)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_stop_words(s: str) -> str:
    return " ".join(w for w in norm(s).split() if w not in {norm(x) for x in _STOP_WORDS})


def parse_float(v) -> float | None:
    """ESHOT '38,39156667' gibi virgüllü ondalık döndürüyor."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None
