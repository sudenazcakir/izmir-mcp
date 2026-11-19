"""Web sohbet sayfası + MCP sunucusu, tek uygulama (Render'da tek servis).

    /           sohbet sayfası (static/index.html)
    /api/chat   soruyu Claude API'ye gönderir; Claude araçları MCP connector ile /mcp üzerinden çağırır
    /mcp        MCP sunucusu (Streamable HTTP), MCP_API_KEY ile korunur
    /health     Render sağlık kontrolü ve demo öncesi uyandırma

Çalıştırma: izmir-mcp-web   (PORT ortam değişkenini okur, varsayılan 8000)

Not: Claude API sunucumuza internetten bağlanır, bu yüzden /api/chat yalnızca herkese açık bir adreste
(Render) uçtan uca çalışır. Yerelde sayfa ve /mcp çalışır, sohbet için PUBLIC_BASE_URL gerekir.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import time
from collections import deque
from importlib import resources
from typing import Any

import anthropic
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import tools
from .config import Settings, get_settings
from .models import StopRef
from .server import mcp
from .sources import eshot

log = logging.getLogger(__name__)

MCP_BETA = "mcp-client-2025-11-20"
MCP_SERVER_NAME = "izmir"
MAX_QUESTION_CHARS = 1000  # kullanıcı mesajı başına
MAX_ASSISTANT_CHARS = 2000  # geçmişteki asistan mesajı başına
MAX_HISTORY_CHARS = 8000  # Claude'a giden geçmişin toplamı (sahte uzun geçmişle maliyet şişirmeye karşı)
MAX_HISTORY_MESSAGES = 12
MAX_RATE_KEYS = 10_000  # bellekte tutulan en fazla IP sayısı
MAX_PAUSE_CONTINUES = 3
RATE_WINDOW_SECONDS = 600
MAX_PLACES = 10  # sayfaya haritada gösterilmek üzere giden en fazla eczane
PLACE_TOOLS = {"find_pharmacies"}
TRIP_TOOL = "get_transit_directions"
LIVE_TOOL = "get_stop_arrivals"
WEATHER_TOOL = "get_weather"
MAX_LIVE_STOPS = 10
MAX_LIVE_BUSES = 12
LIVE_RATE_LIMIT = 60  # canlı harita yenilemesi (15 sn'de bir, 10 dk) için IP başına 10 dakikada
LIVE_TOKEN_SECONDS = 11 * 60  # sayfa 10 dk sonra yenilemeyi bırakıyor
# Canlı harita yenilemelerinin ESHOT'a yapabileceği toplam durak isteği (10 dakikada, tüm kullanıcılar).
# Önbellekte taze olan duraklar sayılmaz. Bütçe dolunca harita "yoğun" der; sohbet ve diğer araçlar etkilenmez.
LIVE_UPSTREAM_BUDGET = 1200
# İzmir ili ve çevresi; bunun dışındaki konumlar yok sayılır (yanlış/sahte koordinata karşı).
IZMIR_BBOX = (37.6, 39.6, 25.8, 28.7)  # min_lat, max_lat, min_lon, max_lon

SYSTEM_PROMPT = """Senin adın CityMate; İzmir için bir şehir asistanısın. \
İzmir Büyükşehir Belediyesi'nin açık verisine bağlı araçların var: \
ESHOT otobüslerinin duraklara tahmini varış süreleri ve canlı konumları, toplu taşıma yol tarifi, nöbetçi eczaneler, \
hava durumu ve giyim önerisi, hava kalitesi ve veri kaynaklarının durumu. \
Yeni araçlar eklendikçe onları da kullan.

- Kullanıcı hangi dilde yazdıysa o dilde cevap ver (çoğunlukla Türkçe).
- Güncel bilgi gereken her soruda aracı çağır; durak, saat, eczane gibi bilgileri asla uydurma.
- Varış süreleri tahmindir, bunu kısaca belirt.
- Bir araç hata döndürürse kaynağın şu an cevap vermediğini açıkça söyle ve elindeki en iyi bilgiyi ver.
- Hata kodu `circuit_open` ise kaynağın art arda hata verdiği için geçici olarak kapalı sayıldığını ve hatadaki \
süreye göre ne zaman yeniden deneneceğini söyle.
- `meta.stale` true ise verinin güncel olmadığını, son alınabilen veri olduğunu belirt.
- Hava kalitesinde iki kaynağı ayır: Open-Meteo değerleri model tahminidir (saatlik); belediye istasyon değerleri \
gerçek ölçümdür ama günlüktür ve birkaç gün gecikmeli gelebilir.
- Cevaplar kısa ve telefonda okunacak kadar sade olsun: önce cevap, sonra gerekirse kısa bir liste.
- İzmir dışı ya da araçlarının kapsamadığı konularda bunu dürüstçe söyle.
- Eczane listesi verdiğinde sayfa onları haritada ve yol tarifi düğmeleriyle ayrıca gösteriyor; cevapta koordinat \
ya da harita linki yazma, eczaneleri yakından uzağa sırala.
- "X'ten Y'ye nasıl giderim" gibi toplu taşıma sorularında get_transit_directions kullan. İzmir'de metro ve \
İZBAN tek hattır: iki uç da aynı sisteme yakınsa (nearby_stations) aktarmasız raylı seçeneği öne çıkar. Önce en \
hızlı olması muhtemel seçeneği, sonra alternatifleri kısaca yaz; kesin süre bilmiyorsan uydurma. Sayfa, seçeneklerin \
sürelerini karşılaştırıp en hızlıyı üstte gösteren toplu taşıma tarifi düğmesini ayrıca gösteriyor; bağlantıyı yazma, \
"aşağıdaki düğmeden süreleri karşılaştırabilirsin" de. Başlangıç bir durağa yakınsa get_stop_arrivals ile oraya \
yaklaşan otobüslere de bakabilirsin.
- Durağa yaklaşan otobüsleri (get_stop_arrivals) verdiğinde sayfa otobüsleri canlı haritada gösterip 15 saniyede bir \
günceller. "Otobüsüm nerede?" gibi sorularda durağı sor ya da konuma göre bul; cevabın sonunda haritadan canlı \
takip edebileceğini kısaca söyle. Konum paylaşıldıysa ve durak söylenmediyse durak yerine en yakın ilçe/semt adını \
kullanabilirsin.
- Hava durumu ve "ne giysem?" sorularında get_weather kullan. Sabah, öğle ve akşam için ayrı ayrı kısa öneri ver: \
sıcaklık/hissedilen, yağmur ihtimali ve aracın `clothing` önerilerini temel al, kendi kafandan sıcaklık uydurma. \
Hava kalitesi (AQI) orta ya da kötüyse bunu da belirt. Geçmiş dönemler listede yoksa yarını önerebilirsin.
- Kullanıcı "yakınımdaki", "bana en yakın" gibi konuma bağlı soruyor ama konum paylaşılmadıysa, sayfanın altındaki \
"Konumumu kullan" düğmesine basmasını ya da ilçe/semt adını yazmasını iste."""

LOCATION_NOTE = (
    "Kullanıcı konumunu paylaştı: enlem {lat:.5f}, boylam {lon:.5f}. \"yakınımdaki\", \"en yakın\" gibi sorularda "
    "ve ilçe söylenmediğinde araçlara bu lat/lon değerlerini ver; uzaklıkları kullanıcıya göre söyle."
)

# Sayfada seçilen arayüz dili. Kullanıcı başka bir dilde yazarsa yine onun dilinde cevap verilir.
LANG_NOTES = {
    "en": (
        "Kullanıcı arayüzü İngilizce kullanıyor. Kullanıcı başka bir dilde yazmadıkça İngilizce cevap ver; "
        "araçlardan gelen Türkçe notları ve giyim önerilerini İngilizceye çevir. Sayfadaki konum düğmesinin adı "
        "\"Use my location\"."
    ),
}


FALLBACKS = {
    "tr": {"refusal": "Bu soruya cevap veremiyorum. Başka bir şekilde sormayı dener misin?",
           "empty": "Şu an bir cevap üretemedim, lütfen tekrar dener misin?"},
    "en": {"refusal": "I can't answer this question. Could you try asking it another way?",
           "empty": "I couldn't produce an answer right now, could you try again?"},
}


def parse_lang(raw: Any) -> str:
    return raw if raw in ("tr", "en") else "tr"


# Sayfa yalnız kendi kaynağımızdan script çalıştırır; harita karoları OpenStreetMap'ten gelir.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",  # sayfadaki tek satır içi script bizim; dış script yok
    "style-src 'self' 'unsafe-inline'",  # Leaflet konumlandırma için satır içi stil kullanıyor
    "img-src 'self' data: https://tile.openstreetmap.org",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
])
PAGE_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(self), camera=(), microphone=()",
}


class McpKeyMiddleware:
    """/mcp isteklerinde `Authorization: Bearer <MCP_API_KEY>` arar.

    Anahtar yoksa: yerelde /mcp açık kalır; herkese açık ortamda (public=True) /mcp 503 döner (fail closed).
    """

    def __init__(self, app: ASGIApp, key: str | None, *, public: bool = False):
        self.app = app
        self.expected = f"Bearer {key}".encode() if key else None
        self.closed = public and not key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp") and self.closed:
            await JSONResponse({"error": "MCP_API_KEY ayarlı değil"}, status_code=503)(scope, receive, send)
            return
        if self.expected and scope["type"] == "http" and scope["path"].startswith("/mcp"):
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class RateLimiter:
    """IP başına ve toplamda kayan pencere. Render ücretsiz katmanı tek örnek çalıştığı için bellekte tutmak yeterli.

    Toplam sınır, IP tespiti yanılsa bile (ör. sahte başlıklar) harcamayı sınırlı tutar.
    """

    def __init__(self, limit: int, global_limit: int, window: float = RATE_WINDOW_SECONDS, max_keys: int = MAX_RATE_KEYS):
        self.limit, self.global_limit, self.window, self.max_keys = limit, global_limit, window, max_keys
        self.hits: dict[str, deque[float]] = {}
        self.all_hits: deque[float] = deque()

    def _trim(self, q: deque[float], now: float) -> None:
        while q and now - q[0] >= self.window:
            q.popleft()

    def _prune(self, now: float) -> None:
        for k in list(self.hits):
            self._trim(self.hits[k], now)
            if not self.hits[k]:
                del self.hits[k]
        while len(self.hits) >= self.max_keys:  # hâlâ çoksa en eski eklenenleri at
            del self.hits[next(iter(self.hits))]

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        self._trim(self.all_hits, now)
        if len(self.all_hits) >= self.global_limit:
            return False
        q = self.hits.get(key)
        if q is not None:
            self._trim(q, now)
            if len(q) >= self.limit:
                return False
        else:
            if len(self.hits) >= self.max_keys:
                self._prune(now)
            q = self.hits[key] = deque()
        q.append(now)
        self.all_hits.append(now)
        return True


class Budget:
    """Pencere başına toplam N birim (ör. ESHOT durak isteği). Ya hepsi ya hiç."""

    def __init__(self, limit: int, window: float = RATE_WINDOW_SECONDS):
        self.limit, self.window = limit, window
        self.used: deque[float] = deque()

    def take(self, n: int) -> bool:
        now = time.monotonic()
        while self.used and now - self.used[0] >= self.window:
            self.used.popleft()
        if len(self.used) + n > self.limit:
            return False
        self.used.extend([now] * n)
        return True


def client_ip(request: Request) -> str:
    """İstemci IP'si: X-Forwarded-For'un SON değeri.

    Render'ın proxy'si gerçek bağlantı IP'sini listenin sonuna ekler; öndeki değerleri istemci kendisi yazabilir.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        last = fwd.split(",")[-1].strip()
        if last:
            return last
    return request.client.host if request.client else "unknown"


def clean_history(raw: Any) -> list[dict[str, str]] | None:
    """Sayfadan gelen geçmişi doğrular: user/assistant sırası, düz metin, son mesaj kullanıcıdan."""
    if not isinstance(raw, list) or not raw:
        return None
    last = raw[-1]
    if not isinstance(last, dict) or last.get("role") != "user" or not isinstance(last.get("content"), str):
        return None
    if not last["content"].strip() or len(last["content"].strip()) > MAX_QUESTION_CHARS:
        return None
    msgs: list[dict[str, str]] = []
    for m in raw[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            return None
        text = m.get("content")
        if not isinstance(text, str) or not text.strip():
            return None
        cap = MAX_QUESTION_CHARS if m["role"] == "user" else MAX_ASSISTANT_CHARS
        msgs.append({"role": m["role"], "content": text.strip()[:cap]})
    # Toplam boyut sınırı: en eskiden at; son kullanıcı mesajı (≤ 1000) her zaman kalır.
    while len(msgs) > 1 and sum(len(m["content"]) for m in msgs) > MAX_HISTORY_CHARS:
        msgs.pop(0)
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def parse_location(raw: Any) -> tuple[float, float] | None:
    """Sayfanın gönderdiği {lat, lon} konumunu doğrular; İzmir dışını ve bozuk değerleri yok sayar."""
    if not isinstance(raw, dict):
        return None
    lat, lon = raw.get("lat"), raw.get("lon")
    for v in (lat, lon):
        if isinstance(v, bool) or not isinstance(v, int | float):
            return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    min_lat, max_lat, min_lon, max_lon = IZMIR_BBOX
    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
        return None
    return float(lat), float(lon)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(getattr(b, "text", "") or "" for b in content)
    return ""


def extract_trip(text: str) -> dict[str, Any] | None:
    """get_transit_directions sonucundan sayfanın göstereceği yol tarifi kartı."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    url = data.get("maps_url")
    if not isinstance(url, str) or not url.startswith("https://www.google.com/maps/dir/"):
        return None
    ends: dict[str, Any] = {}
    for key in ("origin", "destination"):
        p = data.get(key)
        loc = parse_location({"lat": p.get("lat"), "lon": p.get("lon")}) if isinstance(p, dict) else None
        if loc is None or not isinstance(p.get("name"), str):
            return None
        ends[key] = {"name": p["name"][:80], "kind": str(p.get("kind", ""))[:20], "lat": loc[0], "lon": loc[1]}
    near = []
    for n in data.get("nearby_stations") or []:
        if isinstance(n, dict) and isinstance(n.get("name"), str) and n.get("system") in ("metro", "izban", "ferry"):
            near.append({"system": n["system"], "name": n["name"][:60], "end": n.get("end")})
    km = data.get("straight_km")
    return {**ends, "maps_url": url, "nearby": near[:6],
            "straight_km": km if isinstance(km, int | float) and not isinstance(km, bool) else None}


def _num(v: Any) -> float | int | None:
    return v if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else None


def clean_stops(raw: Any) -> list[dict[str, Any]]:
    """Durak listesi: id rakam olmalı; konum İzmir içindeyse tutulur."""
    out: list[dict[str, Any]] = []
    for st in raw if isinstance(raw, list) else []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id", ""))
        if not (sid.isdigit() and len(sid) <= 10):
            continue
        loc = parse_location({"lat": st.get("lat"), "lon": st.get("lon")})
        name = st.get("name") if isinstance(st.get("name"), str) else None
        out.append({"id": sid, "name": name[:80] if name else None,
                    "lat": loc[0] if loc else None, "lon": loc[1] if loc else None})
        if len(out) >= MAX_LIVE_STOPS:
            break
    return out


def live_payload(data: Any) -> dict[str, Any] | None:
    """get_stop_arrivals sonucunu (dict) canlı harita kartına çevirir."""
    if not isinstance(data, dict) or data.get("error"):
        return None
    stops = clean_stops(data.get("stops"))
    if not stops:
        return None
    buses = []
    for a in data.get("arrivals") or []:
        if not isinstance(a, dict) or not isinstance(a.get("line_number"), str):
            continue
        loc = parse_location({"lat": a.get("bus_lat"), "lon": a.get("bus_lon")})
        buses.append({
            "line": a["line_number"][:10],
            "name": str(a.get("line_name") or "")[:80],
            "bus_id": _num(a.get("bus_id")),
            "stop_id": str(a.get("stop_id") or "")[:10],
            "eta": _num(a.get("eta_minutes")),
            "stops_left": _num(a.get("stops_remaining")),
            "lat": loc[0] if loc else None,
            "lon": loc[1] if loc else None,
            "accessible": a.get("wheelchair_accessible") is True,
        })
        if len(buses) >= MAX_LIVE_BUSES:
            break
    return {"stops": stops, "buses": buses}


class LiveSigner:
    """Canlı harita jetonu: sohbet cevabındaki durak listesini imzalar.

    /api/live yalnız bu jetonla çalışır; böylece uç, gelişigüzel durak listeleriyle ESHOT'a açık bir aracıya
    dönüşmez. Jeton, Claude'dan geçmiş (ve sohbet limitine takılan) bir cevaptan gelmek zorunda.
    """

    def __init__(self, secret: bytes):
        self.secret = secret

    def _sig(self, body: bytes) -> str:
        return base64.urlsafe_b64encode(hmac.new(self.secret, body, hashlib.sha256).digest()[:18]).decode()

    def sign(self, stops: list[dict[str, Any]], now: float | None = None) -> str:
        exp = int((now if now is not None else time.time()) + LIVE_TOKEN_SECONDS)
        body = json.dumps({"s": stops, "e": exp}, separators=(",", ":"), ensure_ascii=False).encode()
        return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + self._sig(body)

    def verify(self, token: Any, now: float | None = None) -> list[dict[str, Any]] | None:
        if not isinstance(token, str) or len(token) > 4000 or "." not in token:
            return None
        b64, sig = token.rsplit(".", 1)
        try:
            body = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        except ValueError:
            return None
        if not hmac.compare_digest(sig, self._sig(body)):
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("e"), int):
            return None
        if data["e"] < (now if now is not None else time.time()):
            return None
        stops = clean_stops(data.get("s"))
        return stops or None


def extract_live(text: str) -> dict[str, Any] | None:
    try:
        return live_payload(json.loads(text))
    except ValueError:
        return None


def extract_weather(text: str) -> dict[str, Any] | None:
    """get_weather sonucundan favoriye eklenebilecek yer adı. Kullanıcının kendi konumu (\"Konumun\") verilmez."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    place = data.get("place")
    if not isinstance(place, str) or not place.strip() or place == "Konumun":
        return None
    return {"place": place.strip()[:60]}


def extract_places(text: str) -> list[dict[str, Any]]:
    """find_pharmacies sonucundaki (JSON) konumu bilinen eczaneleri harita için sadeleştirir."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data.get("pharmacies") if isinstance(data, dict) else None
    places: list[dict[str, Any]] = []
    for p in items if isinstance(items, list) else []:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            continue
        loc = parse_location({"lat": p.get("lat"), "lon": p.get("lon")})
        if loc is None:
            continue
        dist = p.get("distance_km")
        places.append({
            "name": p["name"][:120],
            "address": p["address"][:200] if isinstance(p.get("address"), str) else None,
            "phone": p["phone"][:40] if isinstance(p.get("phone"), str) else None,
            "lat": loc[0],
            "lon": loc[1],
            "distance_km": dist if isinstance(dist, int | float) and not isinstance(dist, bool) else None,
        })
    return places[:MAX_PLACES]


async def ask_claude(
    client: anthropic.AsyncAnthropic,
    settings: Settings,
    history: list[dict[str, Any]],
    location: tuple[float, float] | None = None,
    lang: str = "tr",
) -> dict[str, Any]:
    server: dict[str, Any] = {"type": "url", "url": settings.mcp_public_url, "name": MCP_SERVER_NAME}
    if settings.mcp_api_key:
        server["authorization_token"] = settings.mcp_api_key

    messages: list[dict[str, Any]] = list(history)
    texts: list[str] = []
    tools_used: list[str] = []
    tool_names: dict[str, str] = {}
    places: list[dict[str, Any]] = []
    trip: dict[str, Any] | None = None
    live: dict[str, Any] | None = None
    weather: dict[str, Any] | None = None
    system = SYSTEM_PROMPT
    if location:
        system += "\n\n" + LOCATION_NOTE.format(lat=location[0], lon=location[1])
    if lang in LANG_NOTES:
        system += "\n\n" + LANG_NOTES[lang]
    for _ in range(MAX_PAUSE_CONTINUES + 1):
        resp = await client.beta.messages.create(
            model=settings.chat_model,
            max_tokens=settings.chat_max_tokens,
            system=system,
            messages=messages,
            mcp_servers=[server],
            tools=[{"type": "mcp_toolset", "mcp_server_name": MCP_SERVER_NAME}],
            output_config={"effort": settings.chat_effort},
            betas=[MCP_BETA],
        )
        for block in resp.content:
            if block.type == "text" and block.text.strip():
                texts.append(block.text)
            elif block.type == "mcp_tool_use":
                tools_used.append(block.name)
                tool_names[block.id] = block.name
            elif block.type == "mcp_tool_result" and not block.is_error:
                name = tool_names.get(block.tool_use_id)
                if name in PLACE_TOOLS:
                    found = extract_places(_result_text(block.content))
                    if found:
                        places = found  # birden çok arama olduysa sonuncusu (cevabın anlattığı) gösterilir
                elif name == TRIP_TOOL:
                    trip = extract_trip(_result_text(block.content)) or trip
                elif name == LIVE_TOOL:
                    live = extract_live(_result_text(block.content)) or live
                elif name == WEATHER_TOOL:
                    weather = extract_weather(_result_text(block.content)) or weather
        if resp.stop_reason == "refusal":
            return {"answer": FALLBACKS.get(lang, FALLBACKS["tr"])["refusal"], "tools": tools_used,
                    "places": [], "trip": None, "live": None, "weather": None}
        if resp.stop_reason != "pause_turn":
            break
        # Uzun araç zinciri yarıda durdu: cevabı olduğu gibi geri verip devam ettir.
        messages = [*messages, {"role": "assistant", "content": resp.content}]
    answer = "\n\n".join(texts).strip() or FALLBACKS.get(lang, FALLBACKS["tr"])["empty"]
    return {"answer": answer, "tools": tools_used, "places": places, "trip": trip, "live": live, "weather": weather}


def create_app(settings: Settings | None = None, client: anthropic.AsyncAnthropic | None = None) -> ASGIApp:
    settings = settings or get_settings()
    if client is None and settings.anthropic_api_key:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=60)
    limiter = RateLimiter(settings.chat_rate_limit, settings.chat_global_rate_limit)
    live_limiter = RateLimiter(LIVE_RATE_LIMIT, LIVE_RATE_LIMIT * 50)
    upstream = Budget(LIVE_UPSTREAM_BUDGET)  # tüm kullanıcılar ortak
    # Jeton anahtarı süreç başına rastgele: yeniden başlatmada eski jetonlar düşer (harita "tekrar sor" der).
    signer = LiveSigner(os.urandom(32))
    public = bool(settings.public_base_url or settings.render_external_url)
    if public and not settings.mcp_api_key:
        log.error("MCP_API_KEY ayarlı değil: herkese açık ortamda /mcp kapalı (503). Render'da MCP_API_KEY gir.")
    static = resources.files("izmir_mcp").joinpath("static")
    page = static.joinpath("index.html").read_text("utf-8")
    vendor = {  # yalnız bu dosyalar servis edilir (yol gezinmesi yok)
        "leaflet.js": ("application/javascript", static.joinpath("vendor/leaflet/leaflet.js").read_bytes()),
        "leaflet.css": ("text/css", static.joinpath("vendor/leaflet/leaflet.css").read_bytes()),
    }

    async def index(_: Request) -> Response:
        return HTMLResponse(page, headers=PAGE_HEADERS)

    async def leaflet_file(request: Request) -> Response:
        item = vendor.get(request.path_params["name"])
        if item is None:
            return Response("Not Found", status_code=404)
        return Response(item[1], media_type=item[0], headers={
            "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True, "chat": bool(client and settings.mcp_public_url)})

    async def chat(request: Request) -> Response:
        if client is None or not settings.mcp_public_url:
            return JSONResponse(
                {"error": "Sohbet henüz yapılandırılmadı (ANTHROPIC_API_KEY ve PUBLIC_BASE_URL gerekli)."}, status_code=503
            )
        if not limiter.allow(client_ip(request)):
            return JSONResponse({"error": "Çok fazla soru sordun, birkaç dakika sonra tekrar dene."}, status_code=429)
        try:
            body = await request.json()
        except ValueError:
            body = None
        history = clean_history(body.get("messages") if isinstance(body, dict) else None)
        if history is None:
            return JSONResponse({"error": f"Geçersiz istek (soru en fazla {MAX_QUESTION_CHARS} karakter)."}, status_code=400)
        location = parse_location(body.get("location")) if isinstance(body, dict) else None
        lang = parse_lang(body.get("lang")) if isinstance(body, dict) else "tr"
        try:
            result = await ask_claude(client, settings, history, location, lang)
            if result.get("live"):
                result["live"]["token"] = signer.sign(result["live"]["stops"])
            return JSONResponse(result)
        except anthropic.RateLimitError:
            return JSONResponse({"error": "Asistan şu an çok yoğun, biraz sonra tekrar dene."}, status_code=503)
        except anthropic.APIStatusError as e:
            log.warning("Claude API hatası: %s %s", e.status_code, e.message)
            return JSONResponse({"error": "Asistana şu an ulaşılamıyor, biraz sonra tekrar dene."}, status_code=502)
        except anthropic.APIConnectionError:
            log.warning("Claude API'ye bağlanılamadı")
            return JSONResponse({"error": "Asistana şu an ulaşılamıyor, biraz sonra tekrar dene."}, status_code=502)
        except Exception:
            log.exception("Sohbet isteğinde beklenmeyen hata")
            return JSONResponse({"error": "Beklenmeyen bir sorun oluştu, tekrar dener misin?"}, status_code=500)

    async def live(request: Request) -> Response:
        """Canlı harita yenilemesi: Claude'a gitmez. Yalnız sohbetin imzaladığı duraklar, durak başına 10 sn
        önbellekle ve tüm kullanıcılar için ortak bir ESHOT istek bütçesiyle okunur."""
        if not live_limiter.allow(client_ip(request)):
            return JSONResponse({"error": "Çok sık yenilendi, biraz bekle."}, status_code=429)
        try:
            body = await request.json()
        except ValueError:
            body = None
        stops = signer.verify(body.get("token") if isinstance(body, dict) else None)
        if not stops:
            return JSONResponse({"error": "Canlı takip süresi doldu, tekrar sor."}, status_code=403)
        need = sum(1 for st in stops if not eshot.arrivals_fresh(st["id"]))
        if need and not upstream.take(need):
            return JSONResponse({"error": "Canlı harita şu an çok yoğun, biraz sonra tekrar denenecek."},
                                status_code=503)
        refs = [StopRef(**st) for st in stops]
        try:
            result = await tools.arrivals_for_stops("canlı harita", refs, [], MAX_LIVE_BUSES)
        except Exception:
            log.exception("Canlı harita isteğinde beklenmeyen hata")
            return JSONResponse({"error": "Otobüs konumları alınamadı."}, status_code=502)
        payload = live_payload(result.model_dump(mode="json"))
        if payload is None:
            return JSONResponse({"error": "ESHOT şu an cevap vermiyor."}, status_code=502)
        return JSONResponse({"buses": payload["buses"]})

    # MCP uygulaması kendi lifespan'ını (oturum yöneticisi) taşıyor; sayfa ve API yollarını ona ekliyoruz.
    app: Starlette = mcp.streamable_http_app(stateless_http=True, host="0.0.0.0")
    app.add_route("/", index, methods=["GET"])
    app.add_route("/health", health, methods=["GET", "HEAD"])
    app.add_route("/api/chat", chat, methods=["POST"])
    app.add_route("/api/live", live, methods=["POST"])
    app.add_route("/static/vendor/leaflet/{name}", leaflet_file, methods=["GET"])
    return McpKeyMiddleware(app, settings.mcp_api_key, public=public)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    # proxy_headers kapalı: istemci IP'sini yalnız client_ip() belirler, iki mekanizma çelişmez.
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=False)


if __name__ == "__main__":
    main()
