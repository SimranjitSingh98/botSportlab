import requests
import time
import unicodedata
import logging
import re
import os
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

FISR_URL = "https://www.fisr.info/attivita/corsa_risultati.php"

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

def tg_send(testo):
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN non impostato")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": testo, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ── Comandi Telegram ──────────────────────────────────────────────────────────

def normalizza_url(url):
    return url.replace("rollergames.it/media/../corsa/", "rollergames.it/corsa/")

def _url_completo(href):
    if href.startswith("//"):
        return "https:" + href
    if not href.startswith("http"):
        return "https://" + href
    return href

def cmd_gare():
    global _gare_disponibili
    html = scarica(FISR_URL)
    if not html:
        tg_send("❌ Impossibile scaricare la lista gare.")
        return
    soup = BeautifulSoup(html, "html.parser")
    _gare_disponibili = {}
    n = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "rollergames.it" not in href or not href.rstrip("/").endswith("index.htm"):
            continue
        url = normalizza_url(_url_completo(href))
        nome = a.get_text(strip=True)
        if not nome:
            b = a.find("b") or (a.parent.find("b") if a.parent else None)
            nome = b.get_text(strip=True) if b else ""
        if not nome:
            nome = a.parent.get_text(strip=True)[:80] if a.parent else url
        n += 1
        _gare_disponibili[n] = (nome, url)
    if not _gare_disponibili:
        tg_send("_Nessuna gara trovata._")
        return
    lines = ["📋 *Gare disponibili 2025/26:*", ""]
    for num, (nome, _) in _gare_disponibili.items():
        lines.append(f"{num}. {nome}")
    lines += ["", "Rispondi con /segui <numero> per monitorare quella gara"]
    tg_send("\n".join(lines))

def cmd_segui(numero):
    global URL_INDEX, URL_BASE, _snapshot
    if numero not in _gare_disponibili:
        tg_send("❌ Numero non valido. Usa /gare per vedere la lista.")
        return
    nome, url = _gare_disponibili[numero]
    URL_INDEX = url
    URL_BASE  = url.rsplit("/", 1)[0] + "/"
    _snapshot = {}
    log.info(f"URL aggiornato: {URL_INDEX}")
    tg_send(f"✅ Ora monitoro: *{nome}*\nURL: {url}")

def cmd_bacheca():
    html = scarica(FISR_URL)
    if not html:
        tg_send("❌ Impossibile scaricare la pagina.")
        return
    soup = BeautifulSoup(html, "html.parser")
    trovate = []
    for a in soup.find_all("a", href=True):
        if "bacheca_virtuale" not in a["href"]:
            continue
        url = normalizza_url(_url_completo(a["href"]))
        nome = a.get_text(strip=True)
        if not nome and a.parent:
            nome = a.parent.get_text(strip=True)[:80]
        trovate.append((nome or url, url))
    if not trovate:
        tg_send("_Nessuna bacheca virtuale trovata._")
        return
    lines = ["🖥 *Bacheca virtuale:*", ""]
    for nome, url in trovate:
        lines.append(f"[{nome}]({url})")
    tg_send("\n".join(lines))

def _loop_telegram():
    global _tg_offset
    log.info("Telegram polling avviato")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": _tg_offset, "timeout": 30},
                timeout=35,
            )
            for upd in r.json().get("result", []):
                _tg_offset = upd["update_id"] + 1
                testo = upd.get("message", {}).get("text", "").strip()
                if not testo:
                    continue
                log.info(f"Comando ricevuto: {testo}")
                if testo == "/gare":
                    cmd_gare()
                elif testo.startswith("/segui"):
                    parti = testo.split()
                    if len(parti) == 2 and parti[1].isdigit():
                        cmd_segui(int(parti[1]))
                    else:
                        tg_send("Usa: /segui <numero>")
                elif testo == "/bacheca":
                    cmd_bacheca()
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

def formatta_messaggio(cat_label, label_gara, gara, url_gara):
    tipo    = gara.get("tipo", "sconosciuto")
    sezioni = gara.get("sezioni", [])

    lines = [
        f"*{cat_label} — {label_gara}*",
        f"🕐 {ora()}",
        "",
    ]

    if tipo == "batterie":
        batterie_sl = [b for b in sezioni if b["sportlab"]]

        if not batterie_sl:
            lines.append("👟 _Nessun atleta Sport Lab in queste batterie._")
        else:
            lines.append("👟 *Atleti Sport Lab:*")
            for bat in batterie_sl:
                for a in bat["sportlab"]:
                    lines.append(f"  • {a['nome']}  →  Batteria {bat['numero']}")
            lines.append("")

            for bat in batterie_sl:
                lines.append(f"🏁 *Batteria {bat['numero']}*")
                for a in bat["atleti"]:
                    bib  = a["bib"].rjust(3)
                    nome = a["nome"]
                    soc  = a["societa"]
                    if a["sportlab"]:
                        lines.append(f"  {bib}  *{nome}* ★  {soc}")
                    else:
                        lines.append(f"  {bib}  {nome}  {soc}")
                lines.append("")

    elif tipo == "risultati":
        for tab in sezioni:
            sl_righe = [r for r in tab["righe"] if r["sportlab"]]

            if sl_righe:
                lines.append("🏆 *Atleti Sport Lab:*")
                for riga in sl_righe:
                    celle = riga["celle"]
                    pos   = celle[0] if celle else "?"
                    nome  = riga["sportlab"]["nome"]
                    tempo = celle[5] if len(celle) > 5 else ""
                    tempo_str = f"  —  {tempo}" if tempo else ""
                    lines.append(f"  • *{nome}*  →  {pos}°{tempo_str}")
                lines.append("")

            lines.append("📊 *Classifica:*")
            for riga in tab["righe"]:
                celle = riga["celle"]
                pos   = celle[0].rjust(3) if len(celle) > 0 else "  ?"
                nome  = celle[2]           if len(celle) > 2 else ""
                soc   = celle[4]           if len(celle) > 4 else ""
                tempo = celle[5]           if len(celle) > 5 else ""
                star  = " ★" if riga["sportlab"] else "  "
                line  = f"  {pos}.{star}{nome}"
                if soc:
                    line += f"  —  {soc}"
                if tempo:
                    line += f"  —  {tempo}"
                lines.append(line)
            lines.append("")

    else:
        lines.append("_Pagina in aggiornamento..._")
        lines.append("")

    lines.append(f"[🔗 Apri pagina]({url_gara})")
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
            tg_send(formatta_messaggio(cat_label, label_gara, gara, url_gara))
        elif testo != prev:
            log.info(f"AGGIORNAMENTO: {cat_label} — {label_gara}")
            tg_send(formatta_messaggio(cat_label, label_gara, gara, url_gara))
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

    porta = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", porta), HealthHandler)
    log.info(f"HTTP server in ascolto su porta {porta}")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    threading.Thread(target=_loop_monitoraggio, daemon=True).start()
    threading.Thread(target=_loop_telegram, daemon=True).start()

    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
