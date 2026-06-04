import requests
import time
import unicodedata
import logging
import re
import os
import json
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger()

URL_BASE   = "https://attivita.rollergames.it/corsa/bacheca_virtuale/segretari/630840/"
URL_INDEX  = URL_BASE + "index.php"
INTERVALLO = 90

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "867997198")

CATEGORIE_NOSTRE = {
    "RAF": "RAGAZZI F",
    "RAM": "RAGAZZI M",
    "R1F": "RAGAZZI 12 F",
    "R1M": "RAGAZZI 12 M",
}

ATLETI = [
    {"nome": "D'Agostino Lavinia",  "categoria": "RAGAZZI 12 F", "chiavi": ["DAGOSTINO", "LAVINIA"]},
    {"nome": "Gallo Chiara",         "categoria": "RAGAZZI F",    "chiavi": ["GALLO", "CHIARA"]},
    {"nome": "Orefice Federica",     "categoria": "RAGAZZI F",    "chiavi": ["OREFICE", "FEDERICA"]},
    {"nome": "Acciaio Niccolo Lapo", "categoria": "RAGAZZI M",    "chiavi": ["ACCIAIO", "NICCOLO", "LAPO"]},
    {"nome": "Chirico Paolo",        "categoria": "RAGAZZI M",    "chiavi": ["CHIRICO", "PAOLO"]},
    {"nome": "Siani Aldo",           "categoria": "RAGAZZI 12 M", "chiavi": ["SIANI", "ALDO"]},
    {"nome": "Pepe Lorenzo",         "categoria": "RAGAZZI 12 M", "chiavi": ["PEPE", "LORENZO"]},
]

_snapshot         = {}
_gare_disponibili = {}  # {numero: (nome, url)}
_tg_offset        = 0

FISR_URL        = "https://www.fisr.info/attivita/corsa_risultati.php"
INTERVALLO_LIVE = 30
STATO_FILE      = "stato.json"

_live_disponibili          = {}    # {n: {"nome", "url", "aggiornamento"}}
_url_bacheca               = None  # URL bacheca live monitorata
_bacheca_nome              = ""
_bacheca_snapshot          = None
_live_ultimo_aggiornamento = None
_live_prossimo_check       = None
_live_thread_running       = False

def ora():
    return datetime.now().strftime("%H:%M")

def norm(t):
    t = unicodedata.normalize("NFD", t.upper())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")

def cerca_atleta(testo):
    n = norm(testo)
    for a in ATLETI:
        match = sum(1 for k in a["chiavi"] if norm(k) in n)
        if match >= 2:
            return a
    return None

def prefisso_file(href):
    nome = href.split("fn=")[-1].upper() if "fn=" in href else href.split("/")[-1].upper()
    for pref in CATEGORIE_NOSTRE:
        if nome.startswith(pref):
            return pref
    return None

def scarica(url):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.encoding = r.apparent_encoding
        return r.text
    except Exception as e:
        log.error(f"Errore download {url}: {e}")
        return None

def scarica_fisr(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Referer": "https://www.fisr.info/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.encoding = r.apparent_encoding
        return r.text
    except Exception as e:
        log.error(f"Errore download FISR {url}: {e}")
        return None

def tg_send(testo, chat_id=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN non impostato")
        return None
    log.info(f"tg_send — chat_id={cid}, lunghezza={len(testo)}")
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": testo, "parse_mode": "Markdown"},
            timeout=10
        )
        log.info(f"tg_send — status={resp.status_code}, risposta={resp.text[:200]}")
        return resp
    except Exception as e:
        log.error(f"tg_send — ERRORE: {e}")
        return None

def tg_send_lungo(testo, chat_id=None):
    MAX = 4000
    while len(testo) > MAX:
        split = testo.rfind("\n", 0, MAX)
        if split == -1:
            split = MAX
        tg_send(testo[:split], chat_id=chat_id)
        testo = testo[split:].lstrip("\n")
    if testo.strip():
        tg_send(testo, chat_id=chat_id)

# ── Stato persistente ────────────────────────────────────────────────────────

def salva_stato():
    try:
        with open(STATO_FILE, "w") as f:
            json.dump({"url_bacheca": _url_bacheca, "bacheca_nome": _bacheca_nome}, f)
    except Exception as e:
        log.warning(f"salva_stato: {e}")

def carica_stato():
    global _url_bacheca, _bacheca_nome
    try:
        with open(STATO_FILE) as f:
            s = json.load(f)
            _url_bacheca  = s.get("url_bacheca")
            _bacheca_nome = s.get("bacheca_nome", "")
            if _url_bacheca:
                log.info(f"Stato caricato: live su {_url_bacheca}")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"carica_stato: {e}")

# ── Helpers URL ───────────────────────────────────────────────────────────────

def normalizza_url(url):
    return url.replace("rollergames.it/media/../corsa/", "rollergames.it/corsa/")

def _url_completo(href):
    if href.startswith("//"):
        return "https:" + href
    if not href.startswith("http"):
        return "https://" + href
    return href

def _pulisci_sezione(testo):
    return re.sub(r'\s*\(.*', '', testo).strip()

def _testo_prima_di_a(tag):
    """Testo nel nodo padre che precede il tag <a> (= nome gara)."""
    parent = tag.parent
    if not parent:
        return ""
    testi = []
    for child in parent.children:
        if child == tag:
            break
        t = child.get_text(strip=True) if hasattr(child, "get_text") else str(child).strip()
        if t:
            testi.append(t)
    return " ".join(testi).strip()

# ── Parser pagina FISR ────────────────────────────────────────────────────────

def estrai_gare(html):
    """Solo link corsa/rrunn con index.htm — esclude bacheca_virtuale."""
    soup = BeautifulSoup(html, "html.parser")
    gare = {}
    numero = 1
    sezione = "Nazionale"
    for tag in soup.find_all(["b", "a"]):
        if tag.name == "b":
            t = tag.get_text(strip=True)
            if t:
                sezione = _pulisci_sezione(t)
        elif tag.name == "a":
            href = tag.get("href", "")
            if (not href or
                    "rollergames.it" not in href or
                    "index.htm" not in href or
                    "corsa/rrunn" not in href or
                    "bacheca_virtuale" in href):
                continue
            url  = normalizza_url(_url_completo(href))
            nome = _testo_prima_di_a(tag) or sezione
            gare[numero] = {"nome": nome, "url": url, "sezione": sezione}
            numero += 1
    return gare

def estrai_live(html):
    """Solo link bacheca_virtuale."""
    soup = BeautifulSoup(html, "html.parser")
    live = {}
    numero = 1
    for tag in soup.find_all("a", href=True):
        href = tag.get("href", "")
        if "bacheca_virtuale" not in href or "rollergames.it" not in href:
            continue
        url  = normalizza_url(_url_completo(href))
        nome = _testo_prima_di_a(tag)
        if not nome and tag.parent and tag.parent.parent:
            nome = tag.parent.parent.get_text(strip=True)[:80]
        # Cerca data aggiornamento nel testo intorno al link
        testo_intorno = tag.parent.get_text(" ", strip=True) if tag.parent else ""
        m = re.search(r'\d{1,2}/\d{1,2}/\d{2,4}[\s\d:]*', testo_intorno)
        aggiornamento = m.group(0).strip() if m else ""
        live[numero] = {"nome": nome or url, "url": url, "aggiornamento": aggiornamento}
        numero += 1
    return live

# ── Contenuto bacheca live ────────────────────────────────────────────────────

def estrai_contenuto_bacheca(html):
    soup = BeautifulSoup(html, "html.parser")
    lines = []
    h3 = soup.find("h3")
    if h3:
        lines.append(h3.get_text(" ", strip=True))
        lines.append("")
    for bold in soup.find_all("b"):
        ul = bold.find_next_sibling("ul")
        if not ul:
            continue
        cat = bold.get_text(strip=True)
        if cat:
            lines.append(f"*{cat}*")
        for li in ul.find_all("li"):
            t = li.get_text(" ", strip=True)
            if t:
                lines.append(f"  • {t}")
        lines.append("")
    return "\n".join(lines).strip() or soup.get_text(" ", strip=True)[:1500]

# ── Loop bacheca live ─────────────────────────────────────────────────────────

def _loop_bacheca():
    global _bacheca_snapshot, _live_ultimo_aggiornamento, _live_prossimo_check, _live_thread_running
    _live_thread_running = True
    log.info("Loop bacheca live avviato")
    try:
        while _url_bacheca is not None:
            _live_prossimo_check = datetime.now()
            html = scarica(_url_bacheca)
            if html:
                testo = BeautifulSoup(html, "html.parser").get_text(" ")
                if _bacheca_snapshot is None:
                    _bacheca_snapshot = testo
                    log.info("Bacheca live: prima scansione")
                elif testo != _bacheca_snapshot:
                    _bacheca_snapshot = testo
                    _live_ultimo_aggiornamento = datetime.now()
                    ora_str   = _live_ultimo_aggiornamento.strftime("%H:%M:%S")
                    contenuto = estrai_contenuto_bacheca(html)
                    msg = (
                        f"📡 *AGGIORNAMENTO LIVE* — {ora_str}\n"
                        f"{_bacheca_nome}\n\n"
                        f"{contenuto}\n\n"
                        f"[🔗 Apri bacheca live]({_url_bacheca})"
                    )
                    tg_send_lungo(msg)
                    log.info(f"Bacheca live aggiornamento inviato ({ora_str})")
            for _ in range(INTERVALLO_LIVE):
                if _url_bacheca is None:
                    break
                time.sleep(1)
    finally:
        _live_thread_running = False
        log.info("Loop bacheca live terminato")

def _avvia_loop_bacheca():
    if not _live_thread_running:
        threading.Thread(target=_loop_bacheca, daemon=True).start()

# ── Comandi — risultati gare ──────────────────────────────────────────────────

def cmd_gare(chat_id):
    global _gare_disponibili
    log.info("/gare — scarico FISR")
    tg_send("⏳ Scarico lista gare da FISR...", chat_id=chat_id)
    html = scarica_fisr(FISR_URL)
    log.info(f"/gare — html: {len(html) if html else 'None'} car")
    if not html:
        tg_send("❌ Impossibile scaricare da FISR.\nUsa /segui <URL> per impostare direttamente l'URL.", chat_id=chat_id)
        return
    _gare_disponibili = estrai_gare(html)
    log.info(f"/gare — trovati {len(_gare_disponibili)} link")
    if not _gare_disponibili:
        log.warning(f"/gare — HTML preview: {html[:800]}")
        tg_send(f"❌ Parser non trova link.\nHTML preview:\n`{html[:400]}`", chat_id=chat_id)
        return
    lines = ["📋 *Gare disponibili — Risultati e classifiche:*", ""]
    for num, info in _gare_disponibili.items():
        lines.append(f"{num}. [{info['sezione']}] {info['nome']}")
    lines += ["", "Usa /segui <numero> per monitorare una gara"]
    messaggio = "\n".join(lines)
    log.info(f"/gare — {len(messaggio)} car, invio...")
    tg_send_lungo(messaggio, chat_id=chat_id)
    log.info("/gare — DONE")

def cmd_segui(arg, chat_id):
    global URL_INDEX, URL_BASE, _snapshot
    if arg.startswith("http"):
        url  = normalizza_url(arg)
        nome = url
    elif arg.isdigit():
        numero = int(arg)
        if numero not in _gare_disponibili:
            tg_send("❌ Numero non valido. Usa /gare per vedere la lista.", chat_id=chat_id)
            return
        info = _gare_disponibili[numero]
        nome, url = info["nome"], info["url"]
    else:
        tg_send("Usa: /segui <numero> oppure /segui <URL>", chat_id=chat_id)
        return
    URL_INDEX = url
    URL_BASE  = url.rsplit("/", 1)[0] + "/"
    _snapshot = {}
    log.info(f"URL aggiornato: {URL_INDEX}")
    tg_send(f"✅ Ora monitoro: *{nome}*\nURL: {url}", chat_id=chat_id)

# ── Comandi — bacheca live ────────────────────────────────────────────────────

def cmd_live(chat_id):
    global _live_disponibili
    tg_send("⏳ Scarico lista gare live...", chat_id=chat_id)
    html = scarica_fisr(FISR_URL)
    if not html:
        tg_send("❌ Impossibile scaricare la pagina FISR.", chat_id=chat_id)
        return
    _live_disponibili = estrai_live(html)
    if not _live_disponibili:
        tg_send("_Nessuna gara live disponibile al momento._", chat_id=chat_id)
        return
    lines = ["📡 *Gare LIVE disponibili:*", ""]
    for num, info in _live_disponibili.items():
        lines.append(f"{num}. {info['nome']}")
        if info["aggiornamento"]:
            lines.append(f"   Aggiornamento: {info['aggiornamento']}")
    lines += ["", "Usa /seguilive <numero> per seguire una gara live"]
    tg_send_lungo("\n".join(lines), chat_id=chat_id)

def cmd_seguilive(arg, chat_id):
    global _url_bacheca, _bacheca_nome, _bacheca_snapshot
    if arg.startswith("http"):
        url  = normalizza_url(arg)
        nome = url
    elif arg.isdigit():
        numero = int(arg)
        if numero not in _live_disponibili:
            tg_send("❌ Numero non valido. Usa /live per vedere le gare disponibili.", chat_id=chat_id)
            return
        info = _live_disponibili[numero]
        nome, url = info["nome"], info["url"]
    else:
        tg_send("Usa: /seguilive <numero> oppure /seguilive <URL>", chat_id=chat_id)
        return
    _url_bacheca      = url
    _bacheca_nome     = nome
    _bacheca_snapshot = None
    salva_stato()
    _avvia_loop_bacheca()
    log.info(f"Live avviato: {url}")
    tg_send(
        f"📡 Ora seguo LIVE: *{nome}*\n"
        f"Aggiornamento ogni {INTERVALLO_LIVE} secondi.\n"
        f"Usa /stoplive per fermare.",
        chat_id=chat_id
    )

def cmd_stoplive(chat_id):
    global _url_bacheca, _bacheca_snapshot
    _url_bacheca      = None
    _bacheca_snapshot = None
    salva_stato()
    log.info("Live fermato")
    tg_send("⏹ Monitoraggio live fermato.", chat_id=chat_id)

def cmd_statolive(chat_id):
    if not _url_bacheca:
        tg_send("📡 Nessun live attivo. Usa /live per vedere le gare disponibili.", chat_id=chat_id)
        return
    ora_ult = _live_ultimo_aggiornamento.strftime("%H:%M:%S") if _live_ultimo_aggiornamento else "mai"
    pross   = INTERVALLO_LIVE
    if _live_prossimo_check:
        elapsed = (datetime.now() - _live_prossimo_check).total_seconds()
        pross   = max(0, int(INTERVALLO_LIVE - elapsed))
    tg_send(
        f"📡 *Live attivo:* {_bacheca_nome}\n"
        f"Ultimo aggiornamento ricevuto: {ora_ult}\n"
        f"Prossimo controllo: tra {pross}s",
        chat_id=chat_id
    )

def cmd_help(chat_id):
    tg_send(
        "*Comandi disponibili:*\n\n"
        "*Risultati gare:*\n"
        "/gare — lista gare con classifiche\n"
        "/segui <numero|URL> — monitora una gara\n\n"
        "*Bacheca live:*\n"
        "/live — lista gare in diretta\n"
        "/seguilive <numero|URL> — segui una gara live\n"
        "/stoplive — ferma il monitoraggio live\n"
        "/statolive — stato del monitoraggio live",
        chat_id=chat_id
    )

# ── Loop polling Telegram ─────────────────────────────────────────────────────

def _loop_telegram():
    global _tg_offset
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN mancante — polling disabilitato")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook", timeout=10)
        log.info("Webhook eliminato — polling attivo")
    except Exception as e:
        log.warning(f"deleteWebhook fallito: {e}")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": _tg_offset, "timeout": 30},
                timeout=35,
            )
            for upd in r.json().get("result", []):
                _tg_offset = upd["update_id"] + 1
                msg     = upd.get("message", {})
                testo   = msg.get("text", "").strip()
                if not testo:
                    continue
                chat_id = str(msg.get("chat", {}).get("id", TELEGRAM_CHAT_ID))
                cmd     = testo.split()[0].split("@")[0].lower()
                log.info(f"Comando: {testo!r} → {cmd!r} (chat_id={chat_id})")
                if cmd == "/gare":
                    cmd_gare(chat_id=chat_id)
                elif cmd == "/segui":
                    parti = testo.split(None, 1)
                    cmd_segui(parti[1].strip(), chat_id=chat_id) if len(parti) == 2 else tg_send("Usa: /segui <numero> oppure /segui <URL>", chat_id=chat_id)
                elif cmd == "/live":
                    cmd_live(chat_id=chat_id)
                elif cmd == "/seguilive":
                    parti = testo.split(None, 1)
                    cmd_seguilive(parti[1].strip(), chat_id=chat_id) if len(parti) == 2 else tg_send("Usa: /seguilive <numero> oppure /seguilive <URL>", chat_id=chat_id)
                elif cmd == "/stoplive":
                    cmd_stoplive(chat_id=chat_id)
                elif cmd == "/statolive":
                    cmd_statolive(chat_id=chat_id)
                elif cmd == "/help":
                    cmd_help(chat_id=chat_id)
        except Exception as e:
            log.error(f"Telegram polling error: {e}")
            time.sleep(5)

# ── Parser ────────────────────────────────────────────────────────────────────

def leggi_gara(html):
    soup = BeautifulSoup(html, "html.parser")
    testo = soup.get_text(" ").lower()
    h3 = soup.find("h3")
    titolo = h3.get_text(" ", strip=True) if h3 else ""
    if "ordine di partenza" in testo:
        return _leggi_batterie(soup, titolo)
    if "classifica" in testo or "risultat" in testo:
        return _leggi_risultati(soup, titolo)
    return {"titolo": titolo, "tipo": "sconosciuto", "sezioni": []}

def _leggi_batterie(soup, titolo):
    batterie = []
    for elem in soup.find_all(["b", "strong"]):
        m = re.search(r"batteria\s+n\.?\s*(\d+)", elem.get_text(strip=True), re.IGNORECASE)
        if not m:
            continue
        num = int(m.group(1))
        tbl = elem.find_next("table")
        if not tbl:
            continue
        atleti = []
        for row in tbl.find_all("tr"):
            celle = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(celle) >= 3 and celle[2].strip():
                atleti.append({
                    "bib":      celle[0],
                    "nome":     celle[2],
                    "societa":  celle[4] if len(celle) > 4 else "",
                    "sportlab": cerca_atleta(celle[2]),
                })
        if atleti:
            batterie.append({
                "numero":   num,
                "atleti":   atleti,
                "sportlab": [a for a in atleti if a["sportlab"]],
            })
    return {"titolo": titolo, "tipo": "batterie", "sezioni": batterie}

def _leggi_risultati(soup, titolo):
    sezioni = []
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        righe = []
        for row in rows:
            celle = [td.get_text(" ", strip=True) for td in row.find_all("td")]
            if not any(celle):
                continue
            # Salta righe header: la prima cella deve essere un numero (posizione)
            try:
                int(celle[0])
            except (ValueError, IndexError):
                continue
            nome = celle[2] if len(celle) > 2 else ""
            righe.append({"celle": celle, "sportlab": cerca_atleta(nome)})
        if righe:
            sezioni.append({"righe": righe})
    return {"titolo": titolo, "tipo": "risultati", "sezioni": sezioni}

# ── Formattazione messaggi ────────────────────────────────────────────────────

_MEDAGLIE    = {1: "🥇", 2: "🥈", 3: "🥉"}
_PREFISSI_SOC = {"ASD", "SSD", "APS", "APD", "POL", "ARL", "US", "GS", "SS", "AS", "POLVA", "ASDP"}

def abbrevia_societa(soc):
    soc = re.sub(r'\s*\([A-Z0-9]{1,4}\)\s*$', '', soc).strip()
    parole = [p for p in soc.split() if p.replace(".", "").upper() not in _PREFISSI_SOC and p]
    return " ".join(parole[:2]) if parole else soc[:15]

def formatta_messaggio(cat_label, label_gara, gara, url_gara, novita=False):
    tipo    = gara.get("tipo", "sconosciuto")
    sezioni = gara.get("sezioni", [])
    ora_str = datetime.now().strftime("%H:%M:%S")
    prefisso = "🔔 *AGGIORNAMENTO*" if novita else "📥 *Prima scansione*"

    lines = [
        f"{prefisso} — {ora_str}",
        "",
        f"🏁 *{cat_label} — {label_gara}*",
        "",
    ]

    if tipo == "batterie":
        batterie_sl = [b for b in sezioni if b["sportlab"]]
        if not batterie_sl:
            lines.append("_Nessun atleta Sport Lab in queste batterie._")
        else:
            for bat in batterie_sl:
                lines.append(f"*Batteria {bat['numero']}*")
                for i, a in enumerate(bat["atleti"], 1):
                    nome = a["nome"]
                    soc  = abbrevia_societa(a["societa"])
                    soc_str = f" ({soc})" if soc else ""
                    if a["sportlab"]:
                        lines.append(f"{i}. *{nome}*{soc_str} ✅")
                    else:
                        lines.append(f"{i}. {nome}{soc_str}")
                lines.append("")
            n_sl = sum(len(b["sportlab"]) for b in batterie_sl)
            lines.append(f"✅ {n_sl} atleti Sport Lab in questa batteria")

    elif tipo == "risultati":
        for tab in sezioni:
            n_sl = sum(1 for r in tab["righe"] if r["sportlab"])
            for riga in tab["righe"]:
                celle = riga["celle"]
                try:
                    pos = int(celle[0])
                except (ValueError, IndexError):
                    continue
                nome  = celle[2] if len(celle) > 2 else ""
                soc   = abbrevia_societa(celle[4]) if len(celle) > 4 else ""
                tempo = celle[5].strip() if len(celle) > 5 else ""
                pos_str   = _MEDAGLIE.get(pos, f"{pos}.")
                soc_str   = f" ({soc})" if soc else ""
                tempo_str = f" — {tempo}" if tempo else ""
                if riga["sportlab"]:
                    lines.append(f"{pos_str} *{nome}*{soc_str}{tempo_str} ✅")
                else:
                    lines.append(f"{pos_str} {nome}{soc_str}{tempo_str}")
            if n_sl:
                lines.append("")
                lines.append(f"✅ {n_sl} atleti Sport Lab in questa gara")

    else:
        lines.append("_Pagina in aggiornamento..._")

    lines += ["", f"[🔗 Apri risultati]({url_gara})"]
    return "\n".join(lines)

# ── HTTP server (Render health check) ────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SportLab Monitor attivo")

    def log_message(self, format, *args):
        pass

# ── Index e loop principale ───────────────────────────────────────────────────

def get_links(html_index):
    soup = BeautifulSoup(html_index, "html.parser")
    links = []
    for bold in soup.find_all("b"):
        ul = bold.find_next_sibling("ul")
        if not ul:
            continue
        for a in ul.find_all("a"):
            href = a.get("href", "")
            pref = prefisso_file(href)
            if pref:
                full_url = URL_BASE + href if not href.startswith("http") else href
                links.append((full_url, CATEGORIE_NOSTRE[pref], a.get_text(strip=True)))
    return links

def controlla():
    global _snapshot
    html_index = scarica(URL_INDEX)
    if not html_index:
        tg_send("❌ Impossibile raggiungere il sito.")
        return
    links = get_links(html_index)
    if not links:
        log.info("Nessun link trovato per le nostre categorie.")
        return
    for (url_gara, cat_label, label_gara) in links:
        html_gara = scarica(url_gara)
        if not html_gara:
            continue
        testo = BeautifulSoup(html_gara, "html.parser").get_text(" ")
        prev  = _snapshot.get(url_gara)
        _snapshot[url_gara] = testo
        gara  = leggi_gara(html_gara)
        if prev is None:
            log.info(f"Prima scansione: {cat_label} — {label_gara}")
            tg_send_lungo(formatta_messaggio(cat_label, label_gara, gara, url_gara, novita=False))
        elif testo != prev:
            log.info(f"AGGIORNAMENTO: {cat_label} — {label_gara}")
            tg_send_lungo(formatta_messaggio(cat_label, label_gara, gara, url_gara, novita=True))
        else:
            log.info(f"Nessuna modifica: {cat_label} — {label_gara}")

def _loop_monitoraggio():
    tg_send(
        f"✅ *Monitor Sport Lab avviato!*\n"
        f"Categorie: RAF, RAM, R1F, R1M\n"
        f"Intervallo: ogni {INTERVALLO}s\n"
        f"🕐 {ora()}"
    )
    while True:
        try:
            controlla()
        except Exception as e:
            log.error(f"Errore inatteso: {e}")
            tg_send(f"⚠️ Errore inatteso: {e}")
        time.sleep(INTERVALLO)

def main():
    log.info("=" * 50)
    log.info("MONITOR CAMPIONATI ITALIANI 2026 — Sport Lab SA")
    log.info(f"Intervallo: {INTERVALLO}s | Atleti: {len(ATLETI)}")
    log.info("=" * 50)
    log.info(f"TELEGRAM_TOKEN presente: {bool(TELEGRAM_TOKEN)}, lunghezza: {len(TELEGRAM_TOKEN)}")
    log.info(f"TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID}")

    porta = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", porta), HealthHandler)
    log.info(f"HTTP server in ascolto su porta {porta}")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def _test_telegram():
        time.sleep(5)
        log.info("TEST: invio messaggio Telegram di prova")
        result = tg_send("✅ Test avvio — bot online")
        log.info(f"TEST: risultato tg_send = {result}")
    threading.Thread(target=_test_telegram, daemon=True).start()

    carica_stato()
    if _url_bacheca:
        _avvia_loop_bacheca()

    threading.Thread(target=_loop_monitoraggio, daemon=True).start()
    threading.Thread(target=_loop_telegram, daemon=True).start()

    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
