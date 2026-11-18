#!/usr/bin/env python3
"""Radar Freela: acha posts no Threads de gente procurando designer freelancer e manda no Telegram
a DM e o comentário prontos (com o seu portfólio). Você envia; o bot não manda DM sozinho.

Modos:
  python bot.py                 # busca posts, classifica, manda os novos + responde seus comandos
  python bot.py --poll          # só lê e responde seus comandos do Telegram
  python bot.py --dry-run       # imprime em vez de enviar; não grava estado
  python bot.py --mock f.json   # usa posts de um arquivo em vez da busca (teste)
  python bot.py --say "/ajuda"  # simula um comando seu (teste local)
  python bot.py --get-chat-id   # descobre seu chat_id depois de mandar /start pro bot

Somente biblioteca padrão do Python 3.9+.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from itertools import zip_longest
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = Path(os.environ.get("RADAR_STATE", ROOT / "state.json"))
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
GROQ_URL = os.environ.get("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
META = os.environ.get("THREADS_GRAPH", "https://graph.threads.net/v1.0")
SC_URL = os.environ.get("SC_URL", "https://api.scrapecreators.com/v1/threads/search")
NOW = dt.datetime.now(dt.timezone.utc)
AREAS = {"grafico": "🎨 Gráfico", "uiux": "📱 UI/UX", "produto": "📦 Produto", "moveis": "🪑 Móveis", "motion": "🎬 Motion", "outro": "🎯 Design"}


def esc(s):
    return html.escape(str(s))


# ---------------------------------------------------------------- HTTP
def http_json(url, payload=None, headers=None, timeout=60, retries=3):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Content-Type": "application/json", "User-Agent": "radar-freela/1.0 (+https://github.com)", "Accept": "application/json", **(headers or {})}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            last = f"HTTP {e.code}: {body}"
            if e.code not in (429, 500, 502, 503, 504):
                break
            if e.code == 429:
                m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", body)
                wait = (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else 0
                if wait > 90 or "per day" in body or "Request too large" in body:
                    break
                if wait:
                    time.sleep(wait + 1)
                    continue
        except Exception as e:
            last = repr(e)
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(last)


def http_form(url, params, timeout=60):
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(params).encode(),
        headers={"User-Agent": "radar-freela/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


def extract_json_array(text):
    text = re.sub(r"```(?:json)?", "", text)
    i, j = text.find("["), text.rfind("]")
    if i == -1 or j <= i:
        return []
    try:
        return json.loads(text[i : j + 1])
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------- busca de posts
def parse_ts(v):
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            x = float(v)
            x = x / 1000 if x > 1e12 else x
            return dt.datetime.fromtimestamp(x, dt.timezone.utc)
        s = str(v).replace("Z", "+00:00")
        s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s)
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def extract_posts(obj, out=None):
    """Acha posts em qualquer JSON (a ScrapeCreators não documenta o formato da resposta)."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        t = obj.get("text") or obj.get("caption") or obj.get("content")
        if isinstance(t, dict):
            t = t.get("text")
        if isinstance(t, str) and len(t.strip()) >= 15:
            u = obj.get("user") or obj.get("author") or obj.get("owner") or {}
            user = obj.get("username") or (u.get("username") if isinstance(u, dict) else None)
            code = obj.get("code")
            url = obj.get("permalink") or obj.get("url") or obj.get("post_url")
            if not url and user and code:
                url = f"https://www.threads.net/@{user}/post/{code}"
            pid = obj.get("id") or obj.get("pk") or obj.get("post_id") or code or url or t[:40]
            ts = parse_ts(obj.get("timestamp") or obj.get("taken_at") or obj.get("created_at") or obj.get("published_on"))
            out.append({"id": str(pid), "text": t.strip(), "user": user, "url": url, "ts": ts.isoformat() if ts else None,
                        "is_reply": bool(obj.get("is_reply"))})
            return out
        for v in obj.values():
            extract_posts(v, out)
    elif isinstance(obj, list):
        for v in obj:
            extract_posts(v, out)
    return out


def fetch(q):
    tok = os.environ.get("THREADS_ACCESS_TOKEN")
    if tok:  # API oficial da Meta
        params = {
            "q": q, "search_type": "RECENT", "limit": 50, "access_token": tok,
            "since": int(time.time() - CFG.get("max_idade_horas", 72) * 3600),
            "fields": "id,text,permalink,timestamp,username,is_reply,is_quote_post",
        }
        res = http_json(f"{META}/keyword_search?{urllib.parse.urlencode(params)}")
        posts = extract_posts(res)
        for p in posts:
            p["src"] = "meta"
        return posts
    key = os.environ.get("SCRAPECREATORS_API_KEY")
    if key:
        res = http_json(f"{SC_URL}?{urllib.parse.urlencode({'query': q})}", headers={"x-api-key": key})
        posts = extract_posts(res)
        for p in posts:
            p["src"] = "sc"
        return posts
    raise RuntimeError("defina THREADS_ACCESS_TOKEN (API da Meta) ou SCRAPECREATORS_API_KEY")


def pick_queries(st):
    pt, en = CFG["queries_pt"], CFG["queries_en"]
    qs = [x for pair in zip_longest(pt, en) for x in pair if x]
    n = min(CFG.get("queries_por_rodada", 4), len(qs))
    i = st.get("cursor", 0) % len(qs)
    st["cursor"] = (i + n) % len(qs)
    return [qs[(i + k) % len(qs)] for k in range(n)]


# ---------------------------------------------------------------- classificação (Groq)
CLASSIFY = """Classifique posts do Threads. Para cada post, diga se a pessoa está PROCURANDO/CONTRATANDO um designer freelancer \
(projeto, demanda pontual, freela, prestação de serviço de design: gráfico, UI/UX, produto, móveis ou motion): hiring=true.
hiring=false para: designer oferecendo serviços ou divulgando portfólio, vaga fixa CLT/efetiva, curso, venda, meme, opinião, \
promoção, post sem pedido de designer.
Retorne SOMENTE um array JSON com um objeto por post, neste formato:
{"n": <número do post>, "hiring": true|false, "lang": "pt"|"en"|"es"|"outro", "area": "grafico"|"uiux"|"produto"|"moveis"|"motion"|"outro", \
"name": "primeiro nome de quem postou, só se der para deduzir do username ou do texto, senão null", \
"summary": "o que a pessoa procura, em até 100 caracteres, em português"}

Posts:
"""
_BAD = set()


def _chat(model, prompt, key):
    payload = {
        "model": model, "temperature": 0.1,
        "messages": [{"role": "system", "content": "Você classifica posts. Responda só com JSON."}, {"role": "user", "content": prompt}],
        "max_completion_tokens": 900 if model.startswith("qwen/") else 3000,
    }
    if model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "low"
    res = http_json(GROQ_URL, payload, headers={"Authorization": f"Bearer {key}"})
    return extract_json_array(res["choices"][0]["message"]["content"] or "")


HIRE_RE = re.compile(r"(procuro|preciso|busco|buscamos|precisamos|contrat|vaga|looking for|hiring|need(ed)?\b|seeking|wanted)", re.I)
DESIGN_RE = re.compile(r"(design|ilustra|motion|\bux\b|\bui\b|logo|identidade visual|branding|social media)", re.I)
OFFER_RE = re.compile(r"(ofere[çc]o|oferecemos|aceito (jobs|encomendas)|available for|open for (work|commissions)|faço (logo|arte|post))", re.I)
PT_RE = re.compile(r"\b(procuro|preciso|busco|vaga|freela|estou|você|não|uma|para|com|quem|minha|meu|pra|um)\b", re.I)
EN_RE = re.compile(r"\b(the|and|for|looking|hiring|need|with|your|our|we|is|an?|me|my)\b", re.I)


def heuristic(p):
    t = p["text"]
    hiring = bool(HIRE_RE.search(t) and DESIGN_RE.search(t) and not OFFER_RE.search(t))
    return {"hiring": hiring, "lang": "pt" if len(PT_RE.findall(t)) > len(EN_RE.findall(t)) else "en", "area": "outro", "name": None, "summary": t[:100]}


def classify(posts):
    key = os.environ.get("GROQ_API_KEY")
    res = {}
    if key:
        for i in range(0, len(posts), 5):
            chunk = posts[i : i + 5]
            lines = "\n".join(json.dumps({"n": k + 1, "user": p.get("user"), "text": p["text"][:500]}, ensure_ascii=False) for k, p in enumerate(chunk))
            for m in CFG["models"]:
                if m in _BAD:
                    continue
                try:
                    got = _chat(m, CLASSIFY + lines, key)
                except Exception as e:
                    t = str(e)
                    if "model_not_found" in t or "does not exist" in t or "per day" in t:
                        _BAD.add(m)
                    print(f"modelo {m} falhou: {t[:100]}", file=sys.stderr)
                    continue
                for g in got:
                    try:
                        res[chunk[int(g["n"]) - 1]["id"]] = g
                    except (KeyError, ValueError, TypeError, IndexError):
                        pass
                break
            time.sleep(2)
    for p in posts:
        if p["id"] not in res:
            res[p["id"]] = heuristic(p)
    return res


# ---------------------------------------------------------------- mensagens prontas
def clean_name(n):
    if not n or not isinstance(n, str):
        return None
    n = n.strip().split()[0] if n.strip() else ""
    n = re.sub(r"[^\w'-]", "", n)
    if len(n) < 2 or len(n) > 20 or re.search(r"[\d_]", n) or str(n).lower() == "null":
        return None
    return n[:1].upper() + n[1:].lower()


def greet(post):
    pt = post.get("lang") == "pt"
    oi = CFG["oi_pt"] if pt else CFG["oi_en"]
    return f"{oi}, {post['name']}!" if post.get("name") else f"{oi}!"


def link_for(post):
    return CFG["portfolio_pt"] if post.get("lang") == "pt" else CFG["portfolio_en"]


def draft(post, kind):
    pt = post.get("lang") == "pt"
    tpl = CFG[f"{kind}_{'pt' if pt else 'en'}"]
    return tpl.format(oi=greet(post), link=link_for(post))


def ago(iso):
    d = parse_ts(iso)
    if not d:
        return "há ?"
    s = int((dt.datetime.now(dt.timezone.utc) - d).total_seconds())
    return "agora" if s < 120 else f"há {s // 60} min" if s < 3600 else f"há {s // 3600}h" if s < 172800 else f"há {s // 86400} dias"


def fmt_post(p):
    flag = "🇧🇷" if p.get("lang") == "pt" else "🌍"
    user = f" · @{esc(p['user'])}" if p.get("user") else ""
    link = f'🔗 <a href="{html.escape(p["url"], quote=True)}">abrir o post</a>' if p.get("url") else ""
    return (
        f"🎯 <b>#{p['id_n']}</b> · {flag} · {AREAS.get(p.get('area'), AREAS['outro'])}{user} · {ago(p.get('ts'))}\n"
        f"<i>{esc(p.get('summary') or p['text'][:100])}</i>\n{link}\n\n"
        f"💬 <b>DM</b> (toque para copiar):\n<code>{esc(draft(p, 'dm'))}</code>\n\n"
        f"📝 <b>Comentário na thread</b> (use depois de mandar a DM):\n<code>{esc(draft(p, 'comment'))}</code>\n\n"
        f"Depois da DM: /enviei {p['id_n']} · Comentar pelo bot: /comentar {p['id_n']} · "
        f"Corrigir o nome: /nome {p['id_n']} Fulano · Sem interesse: /ignorar {p['id_n']}"
    )


# ---------------------------------------------------------------- Telegram
def tg(method, payload):
    tok = os.environ["TELEGRAM_BOT_TOKEN"]
    return http_json(f"{os.environ.get('TG_API', 'https://api.telegram.org')}/bot{tok}/{method}", payload, timeout=60)


def send(text):
    tg("sendMessage", {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text.strip()[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})


def get_chat_id():
    for u in tg("getUpdates", {}).get("result", []):
        c = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if c:
            print(f"chat_id={c['id']}  ({c.get('first_name') or c.get('title')})")


# ---------------------------------------------------------------- estado e comandos
def load_state():
    st = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    for k, v in (("seen", {}), ("posts", {}), ("offset", 0), ("next_id", 1), ("cursor", 0)):
        st.setdefault(k, v)
    return st


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


HELP = (
    "<b>Comandos</b>\n"
    "/posts: posts que ainda esperam você\n"
    "/texto 12: mostra de novo a DM e o comentário do post #12\n"
    "/enviei 12: marca que você mandou a DM do post #12\n"
    "/comentar 12: publica o comentário no post #12 (precisa mandar a DM antes)\n"
    "/nome 12 Victor: corrige o nome e gera a DM de novo\n"
    "/ignorar 12: esconde o post #12\n"
    "/modelo: mostra seus textos e links\n\n"
    "Respondo em até ~3h. 💅"
)


def meta_reply(post, text):
    tok = os.environ.get("THREADS_ACCESS_TOKEN")
    if not tok:
        raise RuntimeError("falta o THREADS_ACCESS_TOKEN")
    if post.get("src") != "meta":
        raise RuntimeError("esse post veio de outra fonte e não tem o ID da Meta")
    uid = os.environ.get("THREADS_USER_ID") or http_json(f"{META}/me?{urllib.parse.urlencode({'fields': 'id', 'access_token': tok})}")["id"]
    c = http_form(f"{META}/{uid}/threads", {"media_type": "TEXT", "text": text, "reply_to_id": post["raw_id"], "access_token": tok})
    time.sleep(3)
    return http_form(f"{META}/{uid}/threads_publish", {"creation_id": c["id"], "access_token": tok}).get("id")


def get_post(st, tok):
    tok = tok.lstrip("#")
    return st["posts"].get(tok) if tok.isdigit() else None


def handle(text, st):
    m = re.match(r"/(\w+)(?:@\w+)?\s*(.*)", text.strip(), re.S)
    if not m:
        return "Não entendi, sis. Manda /ajuda 💅"
    cmd, arg = m.group(1).lower(), m.group(2).strip()

    if cmd in ("start", "ajuda", "help"):
        return "💋 <b>Radar Freela</b> · SISSS, oi! Eu caço post de gente procurando designer e deixo a DM prontinha com o seu portfólio. Você só cola e manda, babilônica! ✨\n\n" + HELP

    if cmd == "posts":
        pend = [p for p in st["posts"].values() if p["status"] == "novo"]
        if not pend:
            return "Nenhum post esperando, diva. Eu aviso quando achar 💅"
        pend.sort(key=lambda p: p.get("ts") or "", reverse=True)
        rows = [f"#{p['id_n']} · @{esc(p.get('user') or '?')} · {esc(p.get('summary') or '')[:80]} · {ago(p.get('ts'))}" for p in pend[:30]]
        return "<b>Na fila</b>\n" + "\n".join(rows) + "\n\nVer o texto pronto de um post: /texto número"

    if cmd == "modelo":
        return (
            f"<b>DM PT</b>\n<code>{esc(CFG['dm_pt'].format(oi=CFG['oi_pt'] + ', Fulano!', link=CFG['portfolio_pt']))}</code>\n\n"
            f"<b>DM EN</b>\n<code>{esc(CFG['dm_en'].format(oi=CFG['oi_en'] + ', Fulano!', link=CFG['portfolio_en']))}</code>\n\n"
            "Para mudar os textos ou links, edite o config.json no GitHub."
        )

    if cmd in ("enviei", "comentar", "nome", "ignorar", "texto"):
        tok, _, rest = arg.partition(" ")
        p = get_post(st, tok)
        if not p:
            return f"Use /{cmd} 12 (número do post). Veja /posts."
        if cmd == "texto":
            return fmt_post(p)
        if cmd == "enviei":
            p["status"] = "enviado"
            return "ENTREGOU, MÃE! 🔥 DM marcada como enviada. Se quiser, agora o comentário: /comentar " + str(p["id_n"])
        if cmd == "ignorar":
            p["status"] = "ignorado"
            return f"Próxima, sis! 💅 Não aviso mais do post #{p['id_n']}."
        if cmd == "nome":
            n = clean_name(rest)
            if not n:
                return "Use /nome 12 Victor."
            p["name"] = n
            return f"Nome ajustado ✨\n\n💬 <b>DM</b>:\n<code>{esc(draft(p, 'dm'))}</code>\n\n📝 <b>Comentário</b>:\n<code>{esc(draft(p, 'comment'))}</code>"
        # comentar
        if p.get("commented"):
            return "Esse post já recebeu o comentário. Confira lá no Threads 💅"
        if p["status"] != "enviado":
            return "Manda a DM primeiro e marque com /enviei " + str(p["id_n"]) + ", porque o comentário avisa que você mandou DM."
        txt = draft(p, "comment")
        try:
            meta_reply(p, txt)
        except Exception as e:
            return f"Não consegui comentar pela API ({esc(str(e)[:150])}).\nCopie e comente no app:\n<code>{esc(txt)}</code>"
        p["commented"] = True
        return f"Comentário publicado ✅\n<code>{esc(txt)}</code>"

    return "Esse comando eu não conheço, sis. /ajuda 💅"


def run_poll(st):
    for u in tg("getUpdates", {"offset": st["offset"], "timeout": 0}).get("result", []):
        st["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(os.environ["TELEGRAM_CHAT_ID"]):
            continue
        text = (m.get("text") or "").strip()
        if text:
            send(handle(text, st))


# ---------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock")
    ap.add_argument("--say")
    ap.add_argument("--get-chat-id", action="store_true")
    a = ap.parse_args()
    if a.get_chat_id:
        return get_chat_id()
    st = load_state()
    if a.say:
        print(handle(a.say, st))
        if not a.dry_run:
            save_state(st)
        return
    if not a.dry_run:
        run_poll(st)
        if a.poll:
            save_state(st)
            return

    # busca
    raw = []
    if a.mock:
        raw = json.loads(Path(a.mock).read_text(encoding="utf-8"))
    else:
        for q in pick_queries(st):
            try:
                got = fetch(q)
                print(f"'{q}': {len(got)} posts", file=sys.stderr)
                raw.extend(got)
            except Exception as e:
                print(f"'{q}' falhou: {e}", file=sys.stderr)
            time.sleep(1)
    me = (CFG.get("meu_usuario") or "").lower().lstrip("@")
    limite = NOW - dt.timedelta(hours=CFG.get("max_idade_horas", 72))
    novos, vistos = [], set()
    urls = {v.get("url") for v in st["posts"].values() if v.get("url")}
    for p in raw:
        pid = str(p.get("id"))
        if p.get("url") and p["url"] in urls:
            continue
        if p.get("url"):
            urls.add(p["url"])
        d = parse_ts(p.get("ts"))
        if pid in st["seen"] or pid in vistos or p.get("is_reply") or (d and d < limite) or (me and (p.get("user") or "").lower() == me):
            continue
        vistos.add(pid)
        novos.append(p)
    info = classify(novos) if novos else {}
    found = []
    for p in novos:
        st["seen"][str(p["id"])] = NOW.date().isoformat()
        c = info.get(str(p["id"]), {})
        if not c.get("hiring"):
            continue
        lang = c.get("lang") if c.get("lang") in ("pt", "en", "es") else "en"
        found.append({
            "raw_id": str(p["id"]), "text": p["text"], "user": p.get("user"), "url": p.get("url"), "ts": p.get("ts"),
            "src": p.get("src"), "lang": "pt" if lang == "pt" else "en", "area": c.get("area") or "outro",
            "name": clean_name(c.get("name")), "summary": str(c.get("summary") or "")[:110], "status": "novo",
        })
    found = found[: CFG.get("max_posts_por_rodada", 20)]
    for p in found:
        p["id_n"] = st["next_id"]
        st["posts"][str(st["next_id"])] = p
        st["next_id"] += 1
    # limpeza de itens antigos
    corte = (NOW - dt.timedelta(days=14)).date().isoformat()
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= corte}
    st["posts"] = {k: v for k, v in st["posts"].items() if (v.get("ts") or corte)[:10] >= corte or v["status"] == "novo"}

    msgs = []
    if found:
        msgs.append(f"📡 <b>Radar Freela</b> · {len(found)} post(s) procurando designer\n💋 BAFO! Achei, sis. A DM tá prontinha, é só colar e mandar! ✨")
        msgs += [fmt_post(p) for p in found]
    for m_ in msgs:
        if a.dry_run:
            print(m_, "\n" + "=" * 40)
        else:
            send(m_)
            time.sleep(1)
    if not a.dry_run:
        save_state(st)
    print(f"buscados={len(raw)} novos={len(novos)} para_voce={len(found)}", file=sys.stderr)


if __name__ == "__main__":
    main()
