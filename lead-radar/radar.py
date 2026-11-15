#!/usr/bin/env python3
"""Lead Radar: busca en Reddit y Hacker News gente que pide lo que vendes.

Flujo de cada ejecución:
  1. Descarga posts y comentarios recientes de las fuentes configuradas.
  2. Descarta los ya vistos y los que no mencionan ninguna palabra clave.
  3. Claude decide cuáles son intención de compra real, les pone nota (0-10)
     y propone una respuesta útil.
  4. Te avisa por Telegram (si está configurado) y guarda un informe en Markdown.

Nunca publica nada por ti: tú decides si respondes y cómo.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests
import yaml
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "python:lead-radar:v0.1 (personal use)")
HTTP_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------

@dataclass
class Post:
    id: str            # único entre fuentes, p. ej. "reddit:t3_abc" o "hn:123"
    source: str        # "reddit" | "hn"
    where: str         # subreddit o "Hacker News"
    title: str
    body: str
    url: str
    author: str
    created_utc: float
    matched_products: list[str] = field(default_factory=list)

    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()


class LeadVerdict(BaseModel):
    post_id: str = Field(description="El id del post tal cual aparece en la entrada")
    product: str = Field(description="Nombre del producto que encaja, o cadena vacía si ninguno")
    is_lead: bool = Field(description="True solo si la persona busca activamente una solución que el producto resuelve")
    score: int = Field(description="Intención de compra de 0 (nada) a 10 (quiere contratar/comprar ya)")
    reason: str = Field(description="Por qué es o no es un lead, en una frase, en español")
    suggested_reply: str = Field(description="Respuesta propuesta en el idioma del post; vacía si no es lead")


class BatchVerdicts(BaseModel):
    verdicts: list[LeadVerdict]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    """Minúsculas y sin tildes, para comparar palabras clave."""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)  # YAML también lee JSON
    if isinstance(cfg, list):  # proyectos.json: solo la lista de productos
        cfg = {"products": cfg}
    if not cfg or not cfg.get("products"):
        sys.exit(f"{path}: no hay productos configurados")
    cfg.setdefault("settings", {})
    return cfg


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen": {}}


def save_state(path: Path, state: dict, keep_days: int = 14) -> None:
    cutoff = time.time() - keep_days * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=0), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fuentes
# ---------------------------------------------------------------------------

class Reddit:
    """Lectura de Reddit.

    Con REDDIT_CLIENT_ID y REDDIT_CLIENT_SECRET (app de tipo "script", gratis para
    uso personal) usa la API oficial con OAuth. Sin ellas intenta los .json
    públicos y, si Reddit los bloquea, sus feeds RSS públicos.
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.base = "https://www.reddit.com"
        cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
        if cid and secret:
            auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
            r = self.session.post(
                "https://www.reddit.com/api/v1/access_token",
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {auth}"},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            self.session.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
            self.base = "https://oauth.reddit.com"

    @property
    def oauth(self) -> bool:
        return self.base.startswith("https://oauth")

    def _fetch(self, path: str, params: dict) -> list[Post]:
        """API oficial con OAuth; sin claves, .json público y si lo bloquean, RSS."""
        if not self.oauth:
            try:
                return [self._to_post(d) for d in self._get(path, params)]
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code not in (401, 403, 429):
                    raise
            return self._rss(path, params)
        return [self._to_post(d) for d in self._get(path, params)]

    def _get(self, path: str, params: dict) -> list[dict]:
        url = f"{self.base}{path}" if self.oauth else f"{self.base}{path}.json"
        r = self.session.get(url, params={**params, "raw_json": 1}, timeout=HTTP_TIMEOUT)
        if r.status_code == 429 and self.oauth:
            time.sleep(int(float(r.headers.get("retry-after", "10"))))
            r = self.session.get(url, params={**params, "raw_json": 1}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return [c["data"] for c in r.json().get("data", {}).get("children", [])]

    def _rss(self, path: str, params: dict) -> list[Post]:
        """Feed Atom público de Reddit (/new/.rss, /comments/.rss, /search.rss)."""
        import html
        import re
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime

        url = f"https://www.reddit.com{path}.rss" if path == "/search" else f"https://www.reddit.com{path}/.rss"
        r = self.session.get(url, params={k: v for k, v in params.items() if k != "type"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        ns = {"a": "http://www.w3.org/2005/Atom"}
        posts = []
        for e in ET.fromstring(r.content).findall("a:entry", ns):
            rid = (e.findtext("a:id", "", ns) or "").strip()
            link = e.find("a:link", ns)
            url_ = link.get("href", "") if link is not None else ""
            content = e.findtext("a:content", "", ns) or ""
            body = re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", content))).strip()
            body = re.sub(r"submitted by /u/\S+.*$", "", body).strip()
            cat = e.find("a:category", ns)
            when = e.findtext("a:published", "", ns) or e.findtext("a:updated", "", ns)
            try:
                ts = dt.datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
            except ValueError:
                try:
                    ts = parsedate_to_datetime(when).timestamp()
                except (TypeError, ValueError):
                    ts = time.time()
            is_comment = rid.startswith("t1_")
            title = e.findtext("a:title", "", ns) or ""
            posts.append(Post(
                id=f"reddit:{rid}",
                source="reddit",
                where=f"r/{cat.get('term')}" if cat is not None else "Reddit",
                title=re.sub(r"^/u/\S+ on ", "", title) if is_comment else title,
                body=body,
                url=url_,
                author=(e.findtext("a:author/a:name", "", ns) or "").removeprefix("/u/"),
                created_utc=ts,
            ))
        return posts

    @staticmethod
    def _to_post(d: dict) -> Post:
        is_comment = d.get("name", "").startswith("t1_")
        return Post(
            id=f"reddit:{d['name']}",
            source="reddit",
            where=f"r/{d.get('subreddit', '?')}",
            title=d.get("link_title", "") if is_comment else d.get("title", ""),
            body=(d.get("body") if is_comment else d.get("selftext")) or "",
            url="https://www.reddit.com" + d.get("permalink", ""),
            author=d.get("author", ""),
            created_utc=float(d.get("created_utc", 0)),
        )

    def subreddit_new(self, sub: str, limit: int = 100) -> list[Post]:
        return self._fetch(f"/r/{sub}/new", {"limit": limit})

    def subreddit_comments(self, sub: str, limit: int = 100) -> list[Post]:
        return self._fetch(f"/r/{sub}/comments", {"limit": limit})

    def search(self, query: str, limit: int = 50) -> list[Post]:
        return self._fetch("/search", {"q": query, "sort": "new", "t": "day", "limit": limit, "type": "link"})


def hn_search(query: str, since: float, limit: int = 50) -> list[Post]:
    r = requests.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={
            "query": query,
            "tags": "(story,comment)",
            "numericFilters": f"created_at_i>{int(since)}",
            "hitsPerPage": limit,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    posts = []
    for h in r.json().get("hits", []):
        oid = h["objectID"]
        posts.append(Post(
            id=f"hn:{oid}",
            source="hn",
            where="Hacker News",
            title=h.get("title") or h.get("story_title") or "",
            body=h.get("comment_text") or h.get("story_text") or "",
            url=f"https://news.ycombinator.com/item?id={oid}",
            author=h.get("author", ""),
            created_utc=float(h.get("created_at_i", 0)),
        ))
    return posts


def fetch_all(cfg: dict, since: float) -> list[Post]:
    """Descarga de todas las fuentes. Un fallo en una fuente no para las demás."""
    posts: list[Post] = []
    sources = cfg["settings"].get("sources", ["reddit", "hn"])
    reddit = None
    if "reddit" in sources:
        try:
            reddit = Reddit()
        except requests.RequestException as e:
            print(f"[aviso] Reddit no disponible: {e}", file=sys.stderr)

    subs = sorted({s for p in cfg["products"] for s in p.get("subreddits", [])})
    queries = sorted({q for p in cfg["products"] for q in p.get("search_queries", [])})

    def attempt(label: str, fn, *args):
        try:
            got = fn(*args)
            posts.extend(got)
            print(f"  {label}: {len(got)}")
        except requests.RequestException as e:
            print(f"[aviso] {label}: {e}", file=sys.stderr)

    if reddit:
        for sub in subs:
            attempt(f"r/{sub} posts", reddit.subreddit_new, sub)
            if cfg["settings"].get("include_comments", True):
                attempt(f"r/{sub} comentarios", reddit.subreddit_comments, sub)
        for q in queries:
            attempt(f"Reddit búsqueda «{q}»", reddit.search, q)
    if "hn" in sources:
        for q in queries:
            attempt(f"HN «{q}»", hn_search, q, since)

    return [p for p in posts if p.created_utc >= since]


# ---------------------------------------------------------------------------
# Filtro barato por palabras clave
# ---------------------------------------------------------------------------

def prefilter(posts: list[Post], cfg: dict, seen: dict) -> list[Post]:
    out, ids = [], set()
    for p in posts:
        if p.id in seen or p.id in ids:
            continue
        if p.author.lower() in {a.lower() for a in cfg["settings"].get("ignore_authors", [])}:
            continue
        text = normalize(p.text())
        for prod in cfg["products"]:
            kws = [normalize(k) for k in prod.get("keywords", [])]
            neg = [normalize(k) for k in prod.get("negative_keywords", [])]
            if any(k in text for k in kws) and not any(n in text for n in neg):
                p.matched_products.append(prod["name"])
        if p.matched_products:
            out.append(p)
            ids.add(p.id)
    return out


# ---------------------------------------------------------------------------
# Clasificación con Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Eres el filtro de leads de un solopreneur. Recibes posts y comentarios \
públicos de foros y decides cuáles son oportunidades reales de conseguir un cliente \
para alguno de sus productos.

Es lead (is_lead = true) cuando la persona:
- pide recomendaciones o alternativas para un problema que el producto resuelve, o
- se queja de la herramienta que usa ahora y busca otra, o
- describe con detalle un problema que el producto resuelve y pide ayuda.

No es lead: gente que vende algo parecido, noticias, debates generales, memes, \
preguntas que ya tienen solución aceptada, o menciones de pasada.

Nota (score): 9-10 quiere comprar o contratar ya; 7-8 busca solución activamente; \
5-6 problema claro pero sin intención explícita; 0-4 no merece la pena.

La respuesta sugerida debe:
- estar en el idioma del post,
- ayudar de verdad primero (responder a su duda concreta), con 2-5 frases,
- mencionar el producto como una opción, dejando claro que quien responde es su creador,
- no prometer nada que el producto no haga y no sonar a anuncio.

Devuelve un veredicto por cada post recibido, con el mismo post_id."""


def products_block(cfg: dict) -> str:
    lines = ["PRODUCTOS:"]
    for p in cfg["products"]:
        lines.append(f"\n## {p['name']}")
        lines.append(f"Qué es: {p.get('description', '').strip()}")
        if p.get("ideal_customer"):
            lines.append(f"Cliente ideal: {p['ideal_customer'].strip()}")
        if p.get("url"):
            lines.append(f"Web: {p['url']}")
        if p.get("not_a_fit"):
            lines.append(f"No encaja con: {p['not_a_fit'].strip()}")
    return "\n".join(lines)


def classify(posts: list[Post], cfg: dict) -> list[tuple[Post, LeadVerdict]]:
    import anthropic

    s = cfg["settings"]
    model = s.get("model", "claude-opus-5-5")
    batch_size = int(s.get("batch_size", 15))
    max_posts = int(s.get("max_llm_posts_per_run", 120))
    if len(posts) > max_posts:
        print(f"[aviso] {len(posts)} candidatos; solo se analizan los {max_posts} más recientes")
        posts = sorted(posts, key=lambda p: p.created_utc, reverse=True)[:max_posts]

    client = anthropic.Anthropic()
    # El bloque de productos no cambia entre lotes: va en system con caché.
    system = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {"type": "text", "text": products_block(cfg), "cache_control": {"type": "ephemeral"}},
    ]
    by_id = {p.id: p for p in posts}
    results: list[tuple[Post, LeadVerdict]] = []

    for i in range(0, len(posts), batch_size):
        batch = posts[i:i + batch_size]
        payload = [
            {
                "post_id": p.id,
                "donde": p.where,
                "posibles_productos": p.matched_products,
                "titulo": p.title[:300],
                "texto": p.body[:2500],
            }
            for p in batch
        ]
        kwargs = dict(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            output_format=BatchVerdicts,
            output_config={"effort": s.get("effort", "low")},
        )
        # Si la clasificación de seguridad rechaza un lote, la API lo reintenta
        # en otro modelo dentro de la misma llamada (solo en modelos que lo admiten).
        kwargs.update(_client_kwargs(model))

        try:
            resp = client.beta.messages.parse(**kwargs)
        except anthropic.RateLimitError as e:
            wait = int(e.response.headers.get("retry-after", "30"))
            print(f"[aviso] límite de peticiones, espero {wait}s", file=sys.stderr)
            time.sleep(wait)
            resp = client.beta.messages.parse(**kwargs)
        except anthropic.APIStatusError as e:
            print(f"[error] lote {i // batch_size + 1}: {e.status_code} {e.message}", file=sys.stderr)
            continue
        except anthropic.APIConnectionError as e:
            print(f"[error] lote {i // batch_size + 1}: sin conexión ({e})", file=sys.stderr)
            continue

        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            print(f"[aviso] lote {i // batch_size + 1} sin respuesta válida ({resp.stop_reason})", file=sys.stderr)
            continue
        for v in resp.parsed_output.verdicts:
            if v.post_id in by_id:
                results.append((by_id[v.post_id], v))
    return results


INTENT_PHRASES = [
    "recommend", "recommendation", "looking for", "alternative to", "alternatives to",
    "any suggestions", "what do you use", "best tool", "best app", "need a tool", "need an app",
    "is there a tool", "is there an app", "is there any", "any good app", "any good tool", "switching from", "anyone know",
    "recomienda", "recomendais", "recomendáis", "busco", "alguien conoce", "alternativa a",
    "que usais", "qué usáis", "necesito una app", "necesito una herramienta", "alguna app", "alguna herramienta", "hay alguna",
]


def keyword_only(posts: list[Post]) -> list[tuple[Post, LeadVerdict]]:
    """Modo sin IA: palabras clave + frases típicas de intención de compra.

    Se usa con --no-llm, en modo demo y cuando no hay clave de la API de Claude.
    Mucho menos preciso que la IA y sin respuesta sugerida.
    """
    out = []
    for p in posts:
        text = normalize(p.text())
        hits = [ph for ph in INTENT_PHRASES if normalize(ph) in text]
        score = min(9, 5 + 2 * len(hits)) if hits else 4
        reason = (f"Frases de intención: {', '.join(hits[:3])}" if hits
                  else "Solo coincide con palabras clave") + " (sin análisis de IA)"
        out.append((p, LeadVerdict(post_id=p.id, product=p.matched_products[0], is_lead=True,
                                   score=score, reason=reason, suggested_reply="")))
    return out


# ---------------------------------------------------------------------------
# Autoconfiguración y respuestas (usadas por el panel web)
# ---------------------------------------------------------------------------

class ProjectSetup(BaseModel):
    name: str
    description: str = Field(description="Qué es el producto y qué problema resuelve, 2-3 frases")
    ideal_customer: str = Field(description="Quién lo compra, 1-2 frases")
    not_a_fit: str = Field(description="Con quién o qué casos NO encaja, 1 frase")
    keywords: list[str] = Field(description="10-20 palabras o frases cortas que aparecerían en un post de alguien con el problema; en los idiomas de sus clientes")
    negative_keywords: list[str] = Field(description="3-8 palabras que indican que el post NO es un lead (p. ej. 'hiring', 'job', 'I built')")
    subreddits: list[str] = Field(description="5-12 subreddits activos donde su cliente ideal pide ayuda, sin el prefijo r/")
    search_queries: list[str] = Field(description="3-8 búsquedas que encontrarían peticiones de recomendación, p. ej. 'alternative to X'")


def fetch_page_text(url: str, limit: int = 8000) -> str:
    """Texto visible aproximado de una web, para la autoconfiguración."""
    import html
    import re
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    t = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", r.text)
    t = html.unescape(re.sub(r"(?s)<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()[:limit]


def _client_kwargs(model: str) -> dict:
    if model.startswith(("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")):
        return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return {}


def suggest_setup(model: str, url: str = "", description: str = "") -> ProjectSetup:
    import anthropic
    page = ""
    if url:
        try:
            page = fetch_page_text(url)
        except requests.RequestException as e:
            page = f"(No se pudo leer la web: {e})"
    prompt = (
        "Configura un radar de leads para este producto. Devuelve keywords y subreddits "
        "pensados para encontrar a gente que PIDE una solución, no a gente que habla del sector.\n\n"
        f"URL: {url or '(ninguna)'}\n\nDescripción del usuario: {description or '(ninguna)'}\n\n"
        f"Texto de la web:\n{page or '(ninguno)'}"
    )
    resp = anthropic.Anthropic().beta.messages.parse(
        model=model, max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
        output_format=ProjectSetup,
        output_config={"effort": "medium"},
        **_client_kwargs(model),
    )
    if resp.parsed_output is None:
        raise RuntimeError(f"Sin respuesta válida ({resp.stop_reason})")
    return resp.parsed_output


class Reply(BaseModel):
    reply: str


def write_reply(model: str, product: dict, post: dict, tone: str = "") -> str:
    import anthropic
    prompt = (
        f"{products_block({'products': [product]})}\n\n"
        f"POST en {post['where_']}:\nTítulo: {post['title']}\n{post['body'][:3000]}\n\n"
        "Escribe una respuesta para este post siguiendo las reglas de respuesta sugerida. "
        f"{('Tono pedido: ' + tone) if tone else ''}"
    )
    resp = anthropic.Anthropic().beta.messages.parse(
        model=model, max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_format=Reply,
        output_config={"effort": "low"},
        **_client_kwargs(model),
    )
    if resp.parsed_output is None:
        raise RuntimeError(f"Sin respuesta válida ({resp.stop_reason})")
    return resp.parsed_output.reply


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------

def render_markdown(leads: list[tuple[Post, LeadVerdict]], stats: dict) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# Leads · {now}", "",
           f"Descargados: {stats['fetched']} · candidatos: {stats['candidates']} · leads: {len(leads)}", ""]
    for p, v in leads:
        when = dt.datetime.fromtimestamp(p.created_utc, dt.timezone.utc).strftime("%d/%m %H:%M")
        out += [
            f"## [{v.score}/10] {v.product} · {p.where}",
            f"**{p.title or '(comentario)'}** · u/{p.author} · {when}",
            f"{p.url}", "",
            f"> {p.body[:600].strip()}".replace("\n", "\n> "), "",
            f"*Por qué:* {v.reason}", "",
        ]
        if v.suggested_reply:
            out += ["**Respuesta sugerida:**", "", v.suggested_reply.strip(), ""]
        out.append("---")
    return "\n".join(out)


def notify_telegram(leads: list[tuple[Post, LeadVerdict]]) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    for p, v in leads:
        text = (
            f"🎯 {v.score}/10 · {v.product} · {p.where}\n"
            f"{p.title or '(comentario)'}\n\n"
            f"Por qué: {v.reason}\n\n"
            f"Respuesta sugerida:\n{v.suggested_reply}\n\n{p.url}"
        )[:4000]
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                timeout=HTTP_TIMEOUT,
            ).raise_for_status()
        except requests.RequestException as e:
            print(f"[aviso] Telegram: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("LEAD_RADAR_CONFIG", str(HERE / "config.yaml")))
    ap.add_argument("--state", default=str(HERE / "state" / "seen.json"))
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--fixtures", help="JSON con posts de prueba en lugar de descargar de internet")
    ap.add_argument("--no-llm", action="store_true", help="Solo filtro por palabras clave, sin llamar a Claude")
    ap.add_argument("--no-notify", action="store_true", help="No enviar a Telegram")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    s = cfg["settings"]
    state_path = Path(args.state)
    state = load_state(state_path)
    since = time.time() - float(s.get("lookback_hours", 24)) * 3600

    print("Descargando…")
    if args.fixtures:
        raw = json.loads(Path(args.fixtures).read_text(encoding="utf-8"))
        posts = []
        for d in raw:
            d = dict(d)
            age = d.pop("age_min", 0)
            posts.append(Post(**{**d, "created_utc": d.get("created_utc", time.time() - age * 60)}))
    else:
        posts = fetch_all(cfg, since)

    candidates = prefilter(posts, cfg, state["seen"])
    print(f"{len(posts)} descargados, {len(candidates)} candidatos tras el filtro de palabras clave")

    verdicts = keyword_only(candidates) if args.no_llm else (classify(candidates, cfg) if candidates else [])
    min_score = int(s.get("min_score", 7))
    leads = sorted(
        [(p, v) for p, v in verdicts if v.is_lead and v.score >= min_score],
        key=lambda pv: pv[1].score, reverse=True,
    )

    # Solo se marcan como vistos los que Claude llegó a analizar, para no perder
    # posts si un lote falló.
    analysed = {p.id for p, _ in verdicts}
    for p in candidates:
        if p.id in analysed:
            state["seen"][p.id] = p.created_utc
    save_state(state_path, state)

    stats = {"fetched": len(posts), "candidates": len(candidates)}
    md = render_markdown(leads, stats)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"leads-{dt.datetime.now().strftime('%Y%m%d-%H%M')}.md"
    report.write_text(md, encoding="utf-8")
    (out_dir / "latest.json").write_text(
        json.dumps([{"post": asdict(p), "verdict": v.model_dump()} for p, v in leads], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n{len(leads)} leads con nota ≥ {min_score}. Informe: {report}")
    # En GitHub Actions los registros de un repo público los ve cualquiera:
    # allí solo se imprime el recuento y los leads van por Telegram.
    if not os.environ.get("GITHUB_ACTIONS"):
        for p, v in leads:
            print(f"  [{v.score}] {v.product} · {p.where} · {p.title[:70]} · {p.url}")
    if leads and not args.no_notify:
        notify_telegram(leads)
    return 0


if __name__ == "__main__":
    sys.exit(main())
