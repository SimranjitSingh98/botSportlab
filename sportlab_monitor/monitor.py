import requests
import time
import unicodedata
import logging
from datetime import datetime
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger()

URL_BASE   = "https://attivita.rollergames.it/corsa/rrunn/2026/631083/RW00007.1/"
URL_INDEX  = URL_BASE + "index.htm"
INTERVALLO = 90

import os
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

_snapshot = {}

def ora():
    return datetime.now().strftime("%H:%M:%S")

def norm(t):
    t = unicodedata.normalize("NFD", t.upper())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")

def cerca_atleti(testo):
    n = norm(testo)
    trovati = []
    for a in ATLETI:
        match = sum(1 for k in a["chiavi"] if norm(k) in n)
        if match >= 2:
            trovati.append(a)
    return trovati

def prefisso_file(href):
    nome = href.split("/")[-1].upper()
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

def leggi_gara(html):
    soup = BeautifulSoup(html, "html.parser")
    sezioni = []
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        titolo = "Tabella"
        prev = tbl.find_previous_sibling()
        while prev:
            if prev.name in ["b","strong","h1","h2","h3","h4","h5","p","font"]:
                t = prev.get_text(strip=True)
                if len(t) > 2:
                    titolo = t
                    break
            prev = prev.find_previous_sibling()
        headers = [c.get_text(strip=True) for c in rows[0].find_all(["th","td"])]
        dati = []
        for row in rows[1:]:
            celle = [td.get_text(strip=True) for td in row.find_all("td")]
            if any(celle):
                dati.append(celle)
        atleti = cerca_atleti(tbl.get_text(" "))
        sezioni.append({"titolo": titolo, "headers": headers, "dati": dati, "atleti": atleti})
    return sezioni

def formatta_messaggio(cat_label, label_gara, sezioni, url_gara, novita=False):
    prefisso = "🔔 *AGGIORNAMENTO*" if novita else "📥 *Prima scansione*"
    lines = [prefisso, f"*{cat_label} — {label_gara}*", f"🕐 {ora()}", ""]
    if not sezioni:
        lines.append("_Pagina ancora vuota._")
    else:
        for s in sezioni:
            lines.append(f"📋 *{s['titolo']}*")
            if s["dati"]:
                headers = s["headers"]
                for riga in s["dati"][:10]:
                    if headers:
                        parti = [f"{headers[i]}: {riga[i]}" for i in range(min(len(headers), len(riga))) if riga[i]]
                        lines.append("  " + " | ".join(parti))
                    else:
                        lines.append("  " + " | ".join(c for c in riga if c))
                if len(s["dati"]) > 10:
                    lines.append(f"  _...e altri {len(s['dati'])-10} righe_")
            else:
                lines.append("  _Nessun dato ancora._")
            if s["atleti"]:
                lines.append("")
                lines.append("✅ *Atleti Sport Lab:*")
                for a in s["atleti"]:
                    lines.append(f"  • *{a['nome']}* — {a['categoria']}")
            else:
                lines.append("  ℹ️ _Nessun atleta Sport Lab._")
            lines.append("")
    lines.append(f"[Apri pagina]({url_gara})")
    return "\n".join(lines)

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
                links.append((URL_BASE + href, CATEGORIE_NOSTRE[pref], a.get_text(strip=True)))
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
        prev = _snapshot.get(url_gara)
        _snapshot[url_gara] = testo
        sezioni = leggi_gara(html_gara)
        if prev is None:
            log.info(f"Prima scansione: {cat_label} — {label_gara}")
            tg_send(formatta_messaggio(cat_label, label_gara, sezioni, url_gara, novita=False))
        elif testo != prev:
            log.info(f"AGGIORNAMENTO: {cat_label} — {label_gara}")
            tg_send(formatta_messaggio(cat_label, label_gara, sezioni, url_gara, novita=True))
        else:
            log.info(f"Nessuna modifica: {cat_label} — {label_gara}")

def main():
    log.info("=" * 50)
    log.info("MONITOR CAMPIONATI ITALIANI 2026 — Sport Lab SA")
    log.info(f"Intervallo: {INTERVALLO}s | Atleti: {len(ATLETI)}")
    log.info("=" * 50)
    tg_send(f"✅ *Monitor Sport Lab avviato!*\nCategorie: RAF, RAM, R1F, R1M\nIntervallo: ogni {INTERVALLO}s\n🕐 {ora()}")
    while True:
        try:
            controlla()
        except Exception as e:
            log.error(f"Errore inatteso: {e}")
            tg_send(f"⚠️ Errore inatteso: {e}")
        time.sleep(INTERVALLO)

if __name__ == "__main__":
    main()
