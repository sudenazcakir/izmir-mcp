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

import hmac
import logging
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

from .config import Settings, get_settings
from .server import mcp

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

SYSTEM_PROMPT = """Sen İzmir için bir şehir asistanısın. İzmir Büyükşehir Belediyesi'nin açık verisine bağlı araçların var: \
ESHOT otobüslerinin duraklara tahmini varış süreleri, nöbetçi eczaneler, hava kalitesi ve veri kaynaklarının durumu. \
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
- İzmir dışı ya da araçlarının kapsamadığı konularda bunu dürüstçe söyle."""


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


async def ask_claude(client: anthropic.AsyncAnthropic, settings: Settings, history: list[dict[str, Any]]) -> dict[str, Any]:
    server: dict[str, Any] = {"type": "url", "url": settings.mcp_public_url, "name": MCP_SERVER_NAME}
    if settings.mcp_api_key:
        server["authorization_token"] = settings.mcp_api_key

    messages: list[dict[str, Any]] = list(history)
    texts: list[str] = []
    tools_used: list[str] = []
    for _ in range(MAX_PAUSE_CONTINUES + 1):
        resp = await client.beta.messages.create(
            model=settings.chat_model,
            max_tokens=settings.chat_max_tokens,
            system=SYSTEM_PROMPT,
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
        if resp.stop_reason == "refusal":
            return {"answer": "Bu soruya cevap veremiyorum. Başka bir şekilde sormayı dener misin?", "tools": tools_used}
        if resp.stop_reason != "pause_turn":
            break
        # Uzun araç zinciri yarıda durdu: cevabı olduğu gibi geri verip devam ettir.
        messages = [*messages, {"role": "assistant", "content": resp.content}]
    answer = "\n\n".join(texts).strip() or "Şu an bir cevap üretemedim, lütfen tekrar dener misin?"
    return {"answer": answer, "tools": tools_used}


def create_app(settings: Settings | None = None, client: anthropic.AsyncAnthropic | None = None) -> ASGIApp:
    settings = settings or get_settings()
    if client is None and settings.anthropic_api_key:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=60)
    limiter = RateLimiter(settings.chat_rate_limit, settings.chat_global_rate_limit)
    public = bool(settings.public_base_url or settings.render_external_url)
    if public and not settings.mcp_api_key:
        log.error("MCP_API_KEY ayarlı değil: herkese açık ortamda /mcp kapalı (503). Render'da MCP_API_KEY gir.")
    page = resources.files("izmir_mcp").joinpath("static/index.html").read_text("utf-8")

    async def index(_: Request) -> Response:
        return HTMLResponse(page)

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
        try:
            return JSONResponse(await ask_claude(client, settings, history))
        except anthropic.RateLimitError:
            return JSONResponse({"error": "Asistan şu an çok yoğun, biraz sonra tekrar dene."}, status_code=503)
        except anthropic.APIStatusError as e:
            log.warning("Claude API hatası: %s %s", e.status_code, e.message)
            return JSONResponse({"error": "Asistana şu an ulaşılamıyor, biraz sonra tekrar dene."}, status_code=502)
        except anthropic.APIConnectionError:
            log.warning("Claude API'ye bağlanılamadı")
            return JSONResponse({"error": "Asistana şu an ulaşılamıyor, biraz sonra tekrar dene."}, status_code=502)

    # MCP uygulaması kendi lifespan'ını (oturum yöneticisi) taşıyor; sayfa ve API yollarını ona ekliyoruz.
    app: Starlette = mcp.streamable_http_app(stateless_http=True, host="0.0.0.0")
    app.add_route("/", index, methods=["GET"])
    app.add_route("/health", health, methods=["GET", "HEAD"])
    app.add_route("/api/chat", chat, methods=["POST"])
    return McpKeyMiddleware(app, settings.mcp_api_key, public=public)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    # proxy_headers kapalı: istemci IP'sini yalnız client_ip() belirler, iki mekanizma çelişmez.
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=False)


if __name__ == "__main__":
    main()
