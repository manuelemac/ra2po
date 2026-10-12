#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
blackout2podcast
Scarica le trasmissioni di Radio Blackout (radioblackout.org) e ne fa un
podcast tuo: un mp3 per puntata, con i tag giusti, e un feed RSS da aprire
in qualsiasi app di podcast.

Uso:
  python3 blackout2podcast.py                      menu interattivo
  python3 blackout2podcast.py --show SLUG --modo aggiorna --cartella DIR
                                                   senza domande (per cron)

Richiede: Python 3.8+, requests, mutagen. Consigliato: ffmpeg.
Licenza: pubblico dominio (The Unlicense).
"""

import argparse
import contextlib
import html
import json
import os
import re
import shutil
# subprocess: solo liste di argomenti, mai la shell
import subprocess  # nosec B404
import sys
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import quote, urlencode, urljoin, urlsplit
# escape serve solo a scrivere XML, il programma non ne legge mai
from xml.sax.saxutils import escape  # nosec B406

with contextlib.suppress(ImportError):
    import readline  # noqa: F401  frecce e cronologia quando scrivi le risposte

try:
    import mutagen
    import requests
    from mutagen.id3 import COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError
    from mutagen.mp3 import MP3
except ImportError as mancante:
    sys.exit(f"Manca la libreria '{mancante.name}'. Installa con:  pip install requests mutagen")

__version__ = "1.0.0"


# ============================================================================
#  IMPOSTAZIONI FISSE
# ============================================================================

SITO = "https://radioblackout.org"
API = SITO + "/wp-json/wp/v2"
DOMINIO = "radioblackout.org"      # si scarica solo da qui e dai sottodomini
AGENTE = f"Mozilla/5.0 (X11; Linux x86_64) blackout2podcast/{__version__}"
TIMEOUT = (15, 60)                 # secondi: (connessione, lettura)
TENTATIVI = 4                      # prove per ogni richiesta di rete
PAUSA = 0.5                        # secondi tra le richieste: gentilezza verso la radio
MAX_FILE = 2 * 1024 ** 3           # un mp3 non puo' superare 2 GB
MAX_PAGINE = 200                   # tetto alle pagine dell'API, contro risposte assurde
MANIFEST = "episodi.json"          # elenco puntate salvato nella cartella dello show
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

# Caratteri per disegnare: riquadri stile DOS se il terminale capisce UTF-8
if "utf" in (sys.stdout.encoding or "").lower():
    B = {"a_sx": "╔", "a_dx": "╗", "b_sx": "╚", "b_dx": "╝", "oriz": "═", "vert": "║",
         "pieno": "█", "vuoto": "░"}
else:
    B = {"a_sx": "+", "a_dx": "+", "b_sx": "+", "b_dx": "+", "oriz": "-", "vert": "|",
         "pieno": "#", "vuoto": "."}

RADIO = r"""
               ((( o )))
                    \
                     \
                      \
        _______________\_________________
       |  ___________________________    |
       | |:::::::::::::::::::::::::::|   |
       | |::::  RADIO  BLACKOUT  ::::|   |
       | |:::::::::::::::::::::::::::|   |
       | |___________________________|   |
       |   88   92   96  100   105.25    |
       |   |----|----|----|------^--|    |
       |      (@)                (@)     |
       |_________________________________|
          []                         []
"""


class ErroreRete(Exception):
    """Qualsiasi problema nel parlare col sito: rete, risposta, link vietato."""


# ============================================================================
#  TESTO: pulizia, nomi di file, formati
# ============================================================================

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CONTROLLO = re.compile(r"[\x00-\x1f\x7f-\x9f]")
TAG_HTML = re.compile(r"<[^>]+>")
URL_MP3 = re.compile(r'https?://[^\s"\'<>]+?\.mp3', re.IGNORECASE)
LINK_MP3 = re.compile(r'<a\b[^>]*href=["\'][^"\']*\.mp3["\'][^>]*>.*?</a>', re.IGNORECASE | re.DOTALL)
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PREFISSO = re.compile(r"^\d+ - ")


def pulisci(testo):
    """Toglie sequenze ANSI e caratteri di controllo. Un titolo preso dal web
    non deve poter muovere il cursore o cambiare colori nel tuo terminale."""
    return CONTROLLO.sub("", ANSI.sub("", str(testo or "")))


def nome_sicuro(testo, massimo=120):
    """Titolo -> nome di file valido ovunque, senza barre ne' '..'.
    Stessa logica delle versioni precedenti: i file gia' scaricati restano validi."""
    testo = unicodedata.normalize("NFC", str(testo or ""))
    testo = re.sub(r'[<>:"/\\|?*\n\r\t]', " ", testo)
    testo = re.sub(r"\s+", " ", pulisci(testo)).strip()
    return testo[:massimo].rstrip(" .") or "senza-titolo"


def testo_html(frammento):
    """Titolo in HTML -> testo su una riga."""
    testo = html.unescape(TAG_HTML.sub("", str(frammento or "")))
    return pulisci(re.sub(r"\s+", " ", testo)).strip()


def descrizione_da_html(frammento):
    """Corpo HTML della puntata -> testo semplice, senza i link agli mp3."""
    testo = LINK_MP3.sub(" ", str(frammento or "")[:200000])
    testo = re.sub(r"<br\s*/?>", "\n", testo, flags=re.IGNORECASE)
    testo = re.sub(r"</p\s*>", "\n\n", testo, flags=re.IGNORECASE)
    testo = URL_MP3.sub("", html.unescape(TAG_HTML.sub("", testo)))
    righe = [re.sub(r" +", " ", pulisci(r.replace("\t", " "))).strip() for r in testo.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(righe)).strip()


def formato_byte(n):
    """1536 -> '1.5 KB'."""
    n = float(n or 0)
    for unita in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unita == "TB":
            return f"{n:.0f} {unita}" if unita == "B" else f"{n:.1f} {unita}"
        n /= 1024
    return ""


def formato_tempo(secondi):
    """3725 -> '01:02:05'. None -> '--:--:--'."""
    if secondi is None:
        return "--:--:--"
    secondi = int(max(0, secondi))
    return f"{secondi // 3600:02d}:{secondi % 3600 // 60:02d}:{secondi % 60:02d}"


def titolo_da_slug(slug):
    """'bello-come-una-prigione' -> 'Bello Come Una Prigione'."""
    return slug.replace("-", " ").title()


# ============================================================================
#  SICUREZZA: link consentiti e controlli sugli inserimenti
# ============================================================================

def url_consentito(url):
    """Vero solo per link http(s) di radioblackout.org o dei suoi sottodomini.
    Blocca altri siti, credenziali nascoste nel link e porte insolite."""
    try:
        parti = urlsplit(str(url))
        porta = parti.port
    except ValueError:
        return False
    host = (parti.hostname or "").lower()
    return (parti.scheme in ("http", "https")
            and not parti.username and not parti.password
            and porta in (None, 80, 443)
            and (host == DOMINIO or host.endswith("." + DOMINIO)))


def slug_da_testo(testo):
    """Accetta uno slug o un link radioblackout.org/shows/<slug>. None se non valido."""
    testo = str(testo or "").strip()
    if testo.startswith(("http://", "https://")):
        if not url_consentito(testo):
            return None
        pezzi = [p for p in urlsplit(testo).path.split("/") if p]
        if len(pezzi) != 2 or pezzi[0] != "shows":
            return None
        testo = pezzi[1]
    testo = testo.lower()
    return testo if SLUG.match(testo) else None


def valida_cartella(testo):
    """None se il percorso va bene, altrimenti il messaggio d'errore."""
    percorso = os.path.expanduser(testo)
    if not os.path.isabs(percorso):
        return "Serve un percorso completo, es. /mnt/podcast oppure ~/podcast"
    if os.path.exists(percorso) and not os.path.isdir(percorso):
        return "Esiste un file con questo nome: serve una cartella"
    return None


def valida_url_base(testo):
    """Indirizzo pubblico da cui il tuo server web serve la cartella base."""
    try:
        parti = urlsplit(testo)
    except ValueError:
        return "Indirizzo non valido"
    if parti.scheme not in ("http", "https") or not parti.hostname:
        return "Serve un indirizzo che inizia con https:// (es. https://podcast.miosito.it/)"
    if parti.query or parti.fragment or re.search(r"[\s\"'<>`]", testo):
        return "L'indirizzo contiene caratteri non ammessi"
    return None


# ============================================================================
#  CONFIGURAZIONE: ricorda le ultime scelte
# ============================================================================

def percorso_config():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "blackout2podcast", "config.json")


def scrivi_json(percorso, dati, privato=False):
    """Scrive su un file temporaneo e poi lo rinomina: mai file a meta'."""
    temporaneo = percorso + ".tmp"
    rimuovi(temporaneo)
    descrittore = os.open(temporaneo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if privato else 0o644)
    with os.fdopen(descrittore, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=1)
    os.replace(temporaneo, percorso)


def carica_config():
    """Legge le scelte salvate. Tiene solo le chiavi note, e solo se sono testo."""
    try:
        with open(percorso_config(), encoding="utf-8") as f:
            dati = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(dati, dict):
        return {}
    chiavi = ("cartella", "show", "titolo_show", "feed_url")
    return {k: dati[k] for k in chiavi if isinstance(dati.get(k), str)}


def salva_config(config):
    """Salva le scelte, leggibili solo da te (permessi 600)."""
    percorso = percorso_config()
    os.makedirs(os.path.dirname(percorso), mode=0o700, exist_ok=True)
    scrivi_json(percorso, config, privato=True)


# ============================================================================
#  SCHERMO: riquadri, barra, pannello di stato
# ============================================================================

def larghezza():
    """Larghezza del riquadro: segue il terminale, tra 44 e 78 colonne."""
    return max(44, min(78, shutil.get_terminal_size((80, 24)).columns - 1))


def riquadro(righe, titolo=""):
    """Chiude le righe in un riquadro stile DOS. Ritorna una lista di stringhe."""
    larg = larghezza()
    interno = larg - 4
    testa = f" {titolo[:larg - 6]} " if titolo else ""
    sopra = B["a_sx"] + testa.center(larg - 2, B["oriz"]) + B["a_dx"]
    corpo = [f'{B["vert"]} {riga[:interno].ljust(interno)} {B["vert"]}' for riga in righe]
    sotto = B["b_sx"] + B["oriz"] * (larg - 2) + B["b_dx"]
    return [sopra] + corpo + [sotto]


def barra(frazione, lunghezza=30):
    """0.5 -> '[█████░░░░░]  50%'."""
    frazione = min(1.0, max(0.0, frazione))
    pieni = int(frazione * lunghezza)
    return "[" + B["pieno"] * pieni + B["vuoto"] * (lunghezza - pieni) + f"] {int(frazione * 100):3d}%"


class Sistema:
    """Legge CPU, RAM da /proc (Linux). Su altri sistemi mostra 'n/d'."""

    def __init__(self):
        self.precedente = self._contatori_cpu()

    @staticmethod
    def _contatori_cpu():
        try:
            with open("/proc/stat", encoding="ascii") as f:
                valori = [int(x) for x in f.readline().split()[1:]]
            inattivo = valori[3] + (valori[4] if len(valori) > 4 else 0)
            return sum(valori), inattivo
        except (OSError, ValueError, IndexError):
            return None

    def cpu(self):
        """Percentuale di CPU usata dall'ultima lettura."""
        ora, prima = self._contatori_cpu(), self.precedente
        self.precedente = ora
        if not ora or not prima or ora[0] <= prima[0]:
            return "n/d"
        totale = ora[0] - prima[0]
        return f"{100 * (totale - (ora[1] - prima[1])) / totale:3.0f}%"

    @staticmethod
    def ram():
        """RAM usata / totale."""
        try:
            with open("/proc/meminfo", encoding="ascii") as f:
                info = {r.split(":")[0]: int(r.split()[1]) * 1024 for r in f if ":" in r}
            return f"{formato_byte(info['MemTotal'] - info['MemAvailable'])}/{formato_byte(info['MemTotal'])}"
        except (OSError, ValueError, KeyError, IndexError):
            return "n/d"


class Avanzamento:
    """Tiene i conti per la barra: lavoro fatto, velocita', tempo rimasto."""

    def __init__(self, totale):
        self.totale = max(1, totale)
        self.fatte = 0           # elementi conclusi (anche con errore)
        self.parziale = 0.0      # frazione dell'elemento in corso, da 0 a 1
        self.byte = 0            # byte scaricati in tutto
        self.velocita = 0.0      # byte al secondo, media mobile
        self.inizio = time.monotonic()
        self._istante, self._byte = self.inizio, 0
        self.etichetta = ""      # prima riga del riquadro
        self.dettaglio = ""      # seconda riga del riquadro

    def frazione(self):
        return min(1.0, (self.fatte + self.parziale) / self.totale)

    def misura_velocita(self):
        ora = time.monotonic()
        if ora - self._istante >= 0.5:
            istantanea = (self.byte - self._byte) / (ora - self._istante)
            self.velocita = istantanea if not self.velocita else 0.7 * self.velocita + 0.3 * istantanea
            self._istante, self._byte = ora, self.byte

    def tempo_rimasto(self):
        fatto = self.frazione()
        if fatto < 0.01:
            return None
        return (time.monotonic() - self.inizio) * (1 - fatto) / fatto


def righe_stato(av, sistema, cartella):
    """Il contenuto del riquadro di stato."""
    av.misura_velocita()
    try:
        libero = formato_byte(shutil.disk_usage(cartella).free)
    except OSError:
        libero = "n/d"
    return [
        av.etichetta,
        av.dettaglio,
        "",
        barra(av.frazione(), larghezza() - 11),
        f"RETE {formato_byte(av.velocita)}/s   SCARICATI {formato_byte(av.byte)}   "
        f"RESTA {formato_tempo(av.tempo_rimasto())}",
        f"CPU {sistema.cpu()}   RAM {sistema.ram()}   DISCO LIBERO {libero}",
    ]


class Pannello:
    """Riquadro di stato ridisegnato sempre nello stesso punto dello schermo.
    Fuori da un terminale (cron, log su file) stampa solo i messaggi."""

    def __init__(self, titolo):
        self.titolo = titolo
        self.attivo = sys.stdout.isatty()
        self.righe = []          # contenuto attuale del riquadro
        self.altezza = 0         # righe occupate sullo schermo
        self.ultimo = 0.0        # ora dell'ultimo disegno

    def _cancella(self):
        if self.altezza:
            # risale di 'altezza' righe e cancella fino in fondo allo schermo
            sys.stdout.write(f"\x1b[{self.altezza}A\r\x1b[J")
            self.altezza = 0

    def _disegna(self):
        testo = riquadro(self.righe, self.titolo)
        sys.stdout.write("\n".join(testo) + "\n")
        sys.stdout.flush()
        self.altezza = len(testo)

    def mostra(self, righe, subito=False):
        """Aggiorna il riquadro, al massimo due volte al secondo."""
        self.righe = righe
        ora = time.monotonic()
        if self.attivo and (subito or ora - self.ultimo >= 0.5):
            self._cancella()
            self._disegna()
            self.ultimo = ora

    def messaggio(self, testo):
        """Scrive una riga sopra il riquadro, che resta in fondo."""
        if self.attivo:
            self._cancella()
        print(pulisci(testo))
        if self.attivo and self.righe:
            self._disegna()

    def chiudi(self):
        """Lascia sullo schermo l'ultimo stato del riquadro."""
        if self.attivo and self.righe:
            self._cancella()
            self._disegna()
        self.righe, self.altezza = [], 0


# ============================================================================
#  RETE: richieste al sito, elenco trasmissioni e puntate
# ============================================================================

def nuova_sessione():
    sessione = requests.Session()
    sessione.headers["User-Agent"] = AGENTE
    return sessione


def apri(sessione, url, stream=False):
    """GET robusto: timeout, piu' tentativi, redirect seguiti a mano cosi'
    ogni salto resta su radioblackout.org."""
    ultimo_errore = None
    for tentativo in range(TENTATIVI):
        indirizzo = url
        try:
            for _ in range(5):
                if not url_consentito(indirizzo):
                    raise ErroreRete(f"link non consentito: {pulisci(indirizzo)[:80]}")
                risposta = sessione.get(indirizzo, timeout=TIMEOUT, stream=stream, allow_redirects=False)
                if risposta.is_redirect:
                    indirizzo = urljoin(indirizzo, risposta.headers.get("Location", ""))
                    risposta.close()
                    continue
                if risposta.status_code == 429 or risposta.status_code >= 500:
                    risposta.close()
                    raise requests.ConnectionError(f"il sito risponde {risposta.status_code}")
                if risposta.status_code >= 400:
                    risposta.close()
                    raise ErroreRete(f"il sito risponde {risposta.status_code}")
                return risposta
            raise ErroreRete("troppi redirect")
        except (requests.ConnectionError, requests.Timeout) as errore:
            ultimo_errore = errore
            time.sleep(3 * (tentativo + 1))
    raise ErroreRete(f"rete non disponibile dopo {TENTATIVI} tentativi ({ultimo_errore})")


def leggi_json(sessione, risorsa, parametri):
    """Chiama l'API WordPress del sito. Ritorna (dati, pagine_totali)."""
    risposta = apri(sessione, f"{API}/{risorsa}?{urlencode(parametri)}")
    try:
        dati = risposta.json()
        pagine = int(risposta.headers.get("X-WP-TotalPages", "1") or 1)
    except ValueError:
        raise ErroreRete("risposta non valida dal sito") from None
    return dati, min(max(1, pagine), MAX_PAGINE)


def reso(valore):
    """WordPress mette i testi in {'rendered': ...}. Qui prendo il testo."""
    return str(valore.get("rendered", "")) if isinstance(valore, dict) else str(valore or "")


def elenco_show(sessione):
    """Tutte le trasmissioni del sito: lista di (slug, titolo) in ordine alfabetico."""
    trovate, pagina, pagine = [], 1, 1
    while pagina <= pagine:
        dati, pagine = leggi_json(sessione, "shows", {"per_page": 100, "page": pagina, "_fields": "slug,title"})
        for voce in dati if isinstance(dati, list) else []:
            slug = str(voce.get("slug", "")) if isinstance(voce, dict) else ""
            if SLUG.match(slug):
                trovate.append((slug, testo_html(reso(voce.get("title"))) or titolo_da_slug(slug)))
        pagina += 1
        time.sleep(PAUSA)
    return sorted(trovate, key=lambda s: s[1].lower())


def estrai_puntata(voce):
    """Dal JSON di WordPress tiene titolo, data, link mp3 (solo del sito
    della radio) e descrizione. Chiavi in inglese: compatibili con episodi.json."""
    contenuto = reso(voce.get("content"))
    link = []
    for grezzo in URL_MP3.findall(contenuto[:200000]):
        trovato = html.unescape(grezzo)
        if url_consentito(trovato) and trovato not in link:
            link.append(trovato)
    data = str(voce.get("date") or "")[:10]
    return {"title": testo_html(reso(voce.get("title"))) or "senza titolo",
            "date": data if DATA.match(data) else "0000-00-00",
            "mp3s": link,
            "desc": descrizione_da_html(contenuto),
            "link": str(voce.get("link") or "")}


def elenco_puntate(sessione, slug, avviso=print):
    """Tutte le puntate con audio di uno show, dalla piu' vecchia."""
    # Il sito lega le puntate allo show con la tassonomia 'podcastfilter';
    # se manca provo i tag.
    termine = None
    for tassonomia in ("podcastfilter", "tags"):
        dati, _ = leggi_json(sessione, tassonomia, {"slug": slug, "_fields": "id"})
        if isinstance(dati, list) and dati and isinstance(dati[0], dict) and isinstance(dati[0].get("id"), int):
            termine = (tassonomia, dati[0]["id"])
            break
    if not termine:
        raise ErroreRete(f"nessuna puntata trovata per '{slug}'")
    puntate, pagina, pagine = [], 1, 1
    while pagina <= pagine:
        dati, pagine = leggi_json(sessione, "podcast", {
            termine[0]: termine[1], "per_page": 30, "page": pagina,
            "_fields": "link,date,title,content", "orderby": "date", "order": "asc"})
        avviso(f"Leggo l'elenco: pagina {pagina}/{pagine}")
        for voce in dati if isinstance(dati, list) else []:
            if isinstance(voce, dict):
                puntata = estrai_puntata(voce)
                if puntata["mp3s"]:
                    puntate.append(puntata)
        pagina += 1
        time.sleep(PAUSA)
    puntate.sort(key=lambda p: p["date"])
    return puntate


# ============================================================================
#  ELENCO SALVATO (episodi.json) E FILE GIA' PRESENTI
# ============================================================================

def carica_manifest(cartella):
    """Legge episodi.json e scarta le voci malformate. None se non c'e'."""
    try:
        with open(os.path.join(cartella, MANIFEST), encoding="utf-8") as f:
            dati = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(dati, list):
        return None
    buone = []
    for p in dati:
        if not (isinstance(p, dict) and isinstance(p.get("title"), str)
                and isinstance(p.get("date"), str) and isinstance(p.get("mp3s"), list)):
            continue
        link = [u for u in p["mp3s"] if isinstance(u, str) and url_consentito(u)]
        if link:
            buone.append({"title": p["title"], "date": p["date"][:10], "mp3s": link,
                          "desc": str(p.get("desc", "")), "link": str(p.get("link", ""))})
    return buone


def nome_file(numero, puntata):
    """'012 - 2015-03-05 - Titolo della puntata.mp3'."""
    return nome_sicuro(f"{numero:03d} - {puntata['date']} - {puntata['title']}") + ".mp3"


def file_presenti(cartella):
    """Nomi degli mp3 gia' completi, senza il numero iniziale: cosi'
    riconosco una puntata anche se la numerazione cambia."""
    try:
        nomi = os.listdir(cartella)
    except OSError:
        return set()
    return {PREFISSO.sub("", n) for n in nomi if n.lower().endswith(".mp3")}


def data_piu_recente(presenti):
    """La data piu' recente tra i file presenti ('' se non ce ne sono)."""
    date = [n[:10] for n in presenti if DATA.match(n[:10])]
    return max(date, default="")


def rimuovi(percorso):
    with contextlib.suppress(OSError):
        os.remove(percorso)


# ============================================================================
#  AUDIO: download, unione, intestazione, tag
# ============================================================================

def sembra_mp3(percorso):
    """Un mp3 inizia con un tag ID3 o con un frame audio. Una pagina
    d'errore HTML scaricata per sbaglio non passa questo controllo."""
    try:
        with open(percorso, "rb") as f:
            testa = f.read(3)
    except OSError:
        return False
    return testa == b"ID3" or (len(testa) >= 2 and testa[0] == 0xFF and testa[1] & 0xE0 == 0xE0)


def scarica_file(sessione, url, destinazione, progresso):
    """Scarica un mp3. Scrive su .part e rinomina solo alla fine.
    progresso(byte_nuovi, frazione) aggiorna la barra."""
    parziale = destinazione + ".part"
    transitori = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)
    for tentativo in range(TENTATIVI):
        try:
            with apri(sessione, url, stream=True) as risposta:
                try:
                    totale = max(0, int(risposta.headers.get("Content-Length") or 0))
                except ValueError:
                    totale = 0
                if totale > MAX_FILE:
                    raise ErroreRete("file troppo grande")
                scritti = 0
                with open(parziale, "wb") as f:
                    for blocco in risposta.iter_content(65536):
                        scritti += len(blocco)
                        if scritti > MAX_FILE:
                            raise ErroreRete("file troppo grande")
                        f.write(blocco)
                        progresso(len(blocco), scritti / totale if totale else 0.0)
            if not sembra_mp3(parziale):
                raise ErroreRete("il file scaricato non e' un mp3")
            os.replace(parziale, destinazione)
            return
        except transitori:
            time.sleep(3 * (tentativo + 1))
        finally:
            rimuovi(parziale)
    raise ErroreRete("download interrotto troppe volte")


def unisci(pezzi, destinazione):
    """Accoda i pezzi byte per byte. Se i pezzi sono piu' d'uno, dopo serve
    rimuxa(): l'intestazione descrive ancora solo il primo pezzo."""
    with open(destinazione, "wb") as uscita:
        for pezzo in pezzi:
            with open(pezzo, "rb") as ingresso:
                shutil.copyfileobj(ingresso, uscita, 1024 * 1024)


def copia_tag(da, a):
    """Copia i tag ID3 da un file all'altro (titolo, data, descrizione...)."""
    with contextlib.suppress(ID3NoHeaderError):
        ID3(da).save(a, v2_version=3)


def rimuxa(percorso):
    """Riscrive l'intestazione dell'mp3 con ffmpeg, senza ricodificare.
    L'audio resta identico; la durata dichiarata diventa quella vera,
    e i player smettono di fermarsi a meta'."""
    percorso = os.path.abspath(percorso)
    nuovo = percorso + ".remux"
    comando = [FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-protocol_whitelist", "file",
               "-f", "mp3", "-i", percorso, "-map", "0:a", "-c", "copy",
               "-write_xing", "1", "-id3v2_version", "3", "-f", "mp3", nuovo]
    try:
        # binario da shutil.which, argomenti in lista, file nostri: niente shell
        subprocess.run(comando, check=True, timeout=900, capture_output=True)  # noqa: S603  # nosec B603
        copia_tag(percorso, nuovo)
        os.replace(nuovo, percorso)
    finally:
        rimuovi(nuovo)


def durate(percorso):
    """(durata dichiarata dall'intestazione, durata vera contando i frame).
    Conta senza decodificare: circa 0.2 secondi per un'ora di audio."""
    comando = [FFPROBE, "-v", "error", "-protocol_whitelist", "file", "-f", "mp3", "-count_packets",
               "-select_streams", "a:0", "-show_entries", "format=duration:stream=nb_read_packets,sample_rate",
               "-of", "json", os.path.abspath(percorso)]
    try:
        uscita = subprocess.run(comando, check=True, timeout=300,  # noqa: S603  # nosec B603
                                capture_output=True, text=True)
        dati = json.loads(uscita.stdout)
        flusso = dati["streams"][0]
        frequenza = int(flusso["sample_rate"])
        campioni = 1152 if frequenza >= 32000 else 576   # campioni per frame MP3
        return float(dati["format"]["duration"]), int(flusso["nb_read_packets"]) * campioni / frequenza
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
        return None


def da_riparare(dichiarata, vera):
    """Vero se l'intestazione sbaglia di oltre 2 secondi (o dell'1%)."""
    return abs(vera - dichiarata) > max(2.0, vera * 0.01)


def tagga(percorso, puntata, numero, album):
    """Scrive i tag ID3: titolo, data, numero, show, descrizione."""
    try:
        tag = ID3(percorso)
    except ID3NoHeaderError:
        tag = ID3()
    tag.add(TIT2(encoding=3, text=puntata["title"]))
    tag.add(TALB(encoding=3, text=album))
    tag.add(TPE1(encoding=3, text=album))
    tag.add(TPE2(encoding=3, text=album))
    tag.add(TCON(encoding=3, text="Podcast"))
    tag.add(TRCK(encoding=3, text=str(numero)))
    if DATA.match(puntata["date"]) and puntata["date"] != "0000-00-00":
        tag.add(TDRC(encoding=3, text=puntata["date"]))
    if puntata.get("desc"):
        tag.add(COMM(encoding=3, lang="ita", desc="", text=puntata["desc"][:4000]))
    tag.save(percorso, v2_version=3)


def scarica_puntata(sessione, puntata, *, numero, finale, temporanei, album, av, aggiorna):
    """Scarica i pezzi di una puntata, li unisce, sistema l'intestazione e
    scrive i tag. Il file finale compare solo quando e' completo."""
    pezzi, numero_pezzi = [], len(puntata["mp3s"])
    unito = os.path.join(temporanei, f"{numero:04d}_unito.mp3")
    try:
        for indice, url in enumerate(puntata["mp3s"]):
            av.dettaglio = f"pezzo {indice + 1}/{numero_pezzi}"
            pezzo = os.path.join(temporanei, f"{numero:04d}_{indice}.mp3")

            def progresso(nuovi, frazione, indice=indice):
                av.byte += nuovi
                av.parziale = (indice + frazione) / numero_pezzi
                aggiorna()

            scarica_file(sessione, url, pezzo, progresso)
            pezzi.append(pezzo)
            time.sleep(PAUSA)
        unisci(pezzi, unito)
        if numero_pezzi > 1:
            av.dettaglio = "sistemo l'intestazione con ffmpeg"
            aggiorna()
            rimuxa(unito)
        tagga(unito, puntata, numero, album)
        os.replace(unito, finale)
    finally:
        for pezzo in pezzi + [unito]:
            rimuovi(pezzo)


# ============================================================================
#  FEED RSS
# ============================================================================

def leggi_tag(percorso):
    """Titolo, data, descrizione e durata di un mp3, dai suoi tag."""
    info = {"titolo": os.path.splitext(os.path.basename(percorso))[0], "data": "", "descrizione": "", "durata": 0}
    try:
        audio = MP3(percorso)
        info["durata"] = int(audio.info.length)
        tag = audio.tags
        if tag is not None:
            if "TIT2" in tag and tag["TIT2"].text:
                info["titolo"] = str(tag["TIT2"].text[0])
            if "TDRC" in tag and tag["TDRC"].text:
                info["data"] = str(tag["TDRC"].text[0])[:10]
            commenti = tag.getall("COMM")
            if commenti and commenti[0].text:
                info["descrizione"] = str(commenti[0].text[0])
    except (mutagen.MutagenError, OSError):
        pass
    return info


def data_rss(data, percorso):
    """Data in formato RSS. Senza data valida uso la data del file."""
    try:
        momento = datetime.strptime(data, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
    except ValueError:
        momento = datetime.fromtimestamp(os.path.getmtime(percorso), tz=timezone.utc)
    return format_datetime(momento)


def crea_feed(cartella, url_cartella, titolo, link, autore="Radio Blackout 105.250 FM"):
    """Scrive feed.xml nella cartella. url_cartella e' l'indirizzo pubblico
    da cui il tuo server web serve quella cartella. Ritorna il numero di puntate."""
    base = url_cartella.rstrip("/") + "/"

    def testo(valore):          # testo sicuro dentro un tag XML
        righe = [pulisci(r) for r in str(valore).split("\n")]
        return escape("\n".join(righe))

    def attributo(valore):      # testo sicuro dentro un attributo XML
        return escape(pulisci(valore), {'"': "&quot;"})

    nomi = sorted(n for n in os.listdir(cartella) if n.lower().endswith(".mp3") and not n.startswith("."))
    voci = []
    for nome in reversed(nomi):          # dalla puntata piu' recente
        percorso = os.path.join(cartella, nome)
        info = leggi_tag(percorso)
        indirizzo = base + quote(nome)
        ore, resto = divmod(info["durata"], 3600)
        voci.append(
            "    <item>\n"
            f"      <title>{testo(info['titolo'])}</title>\n"
            f'      <guid isPermaLink="false">{testo(indirizzo)}</guid>\n'
            f"      <pubDate>{data_rss(info['data'], percorso)}</pubDate>\n"
            f'      <enclosure url="{attributo(indirizzo)}" length="{os.path.getsize(percorso)}" type="audio/mpeg"/>\n'
            f"      <itunes:duration>{ore}:{resto // 60:02d}:{resto % 60:02d}</itunes:duration>\n"
            f"      <description>{testo(info['descrizione'])}</description>\n"
            "    </item>")

    immagine = ""
    for copertina in ("cover.jpg", "cover.jpeg", "cover.png"):
        if os.path.isfile(os.path.join(cartella, copertina)):
            immagine = f'    <itunes:image href="{attributo(base + copertina)}"/>\n'
            break

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
           'xmlns:atom="http://www.w3.org/2005/Atom">\n'
           "  <channel>\n"
           f"    <title>{testo(titolo)}</title>\n"
           f"    <link>{testo(link)}</link>\n"
           "    <language>it</language>\n"
           f"    <description>{testo(titolo)} - {testo(autore)}</description>\n"
           f"    <itunes:author>{testo(autore)}</itunes:author>\n"
           "    <itunes:explicit>false</itunes:explicit>\n"
           f'    <atom:link href="{attributo(base + "feed.xml")}" rel="self" type="application/rss+xml"/>\n'
           f"{immagine}"
           + "\n".join(voci) + "\n"
           "  </channel>\n"
           "</rss>\n")
    temporaneo = os.path.join(cartella, "feed.xml.tmp")
    with open(temporaneo, "w", encoding="utf-8") as f:
        f.write(xml)
    os.replace(temporaneo, os.path.join(cartella, "feed.xml"))
    return len(voci)


# ============================================================================
#  AZIONI: scarica, aggiorna, ripara
# ============================================================================

def scarica(slug, titolo_show, cartella, solo_nuove=False):
    """Scarica nella cartella le puntate che mancano. Con solo_nuove prende
    solo quelle uscite dopo la piu' recente che hai. Ritorna gli errori."""
    os.makedirs(cartella, exist_ok=True)
    temporanei = os.path.join(cartella, ".tmp")
    if os.path.islink(temporanei):
        os.remove(temporanei)
    shutil.rmtree(temporanei, ignore_errors=True)       # avanzi di un giro interrotto
    os.makedirs(temporanei, exist_ok=True)
    pannello = Pannello(f"BLACKOUT2PODCAST {__version__}")
    sessione = nuova_sessione()

    puntate = None if solo_nuove else carica_manifest(cartella)
    if puntate:
        pannello.messaggio(f"Elenco letto da {MANIFEST}: {len(puntate)} puntate (nessuna scansione del sito)")
    else:
        pannello.messaggio("Leggo l'elenco delle puntate dal sito ...")
        puntate = elenco_puntate(sessione, slug, pannello.messaggio)
        scrivi_json(os.path.join(cartella, MANIFEST), puntate)
        pannello.messaggio(f"Trovate {len(puntate)} puntate. Elenco salvato in {MANIFEST}")

    presenti = file_presenti(cartella)
    dal_giorno = data_piu_recente(presenti) if solo_nuove else ""
    da_fare, senza_ffmpeg = [], 0
    for numero, puntata in enumerate(puntate, 1):
        nome = nome_file(numero, puntata)
        if PREFISSO.sub("", nome) in presenti or puntata["date"] < dal_giorno:
            continue
        if len(puntata["mp3s"]) > 1 and not FFMPEG:
            senza_ffmpeg += 1
            continue
        da_fare.append((numero, puntata, nome))
    if senza_ffmpeg:
        pannello.messaggio(f"Salto {senza_ffmpeg} puntate divise in piu' pezzi: per unirle serve ffmpeg.")
    if not da_fare:
        pannello.messaggio("Niente da scaricare: sei in pari.")
        shutil.rmtree(temporanei, ignore_errors=True)
        return 0
    pannello.messaggio(f"Da scaricare: {len(da_fare)} puntate. CTRL+C per interrompere.")

    av, sistema, errori = Avanzamento(len(da_fare)), Sistema(), 0

    def aggiorna():
        pannello.mostra(righe_stato(av, sistema, cartella))

    try:
        for posizione, (numero, puntata, nome) in enumerate(da_fare, 1):
            av.etichetta = f"PUNTATA {posizione}/{len(da_fare)}  {pulisci(puntata['title'])}"
            aggiorna()
            try:
                scarica_puntata(sessione, puntata, numero=numero, finale=os.path.join(cartella, nome),
                                temporanei=temporanei, album=titolo_show, av=av, aggiorna=aggiorna)
                pannello.messaggio(f"  OK   {nome}")
            except (ErroreRete, OSError, subprocess.SubprocessError, mutagen.MutagenError) as errore:
                errori += 1
                pannello.messaggio(f"  ERR  {nome[:45]}: {pulisci(errore)[:60]}")
            av.fatte, av.parziale = av.fatte + 1, 0.0
        av.dettaglio = "fatto"
        pannello.mostra(righe_stato(av, sistema, cartella), subito=True)
        pannello.chiudi()
    except KeyboardInterrupt:
        pannello.chiudi()
        print("\nINTERROTTO. I file completi restano: rilancia per riprendere.")
        errori += 1
    finally:
        shutil.rmtree(temporanei, ignore_errors=True)
    print(f"\n{av.fatte} puntate lavorate, {errori} errori.")
    return errori


def ripara(cartella):
    """Controlla gli mp3 e sistema quelli con l'intestazione sbagliata
    (i player si fermano a meta'). Lavora in locale, non scarica nulla."""
    if not (FFMPEG and FFPROBE):
        print("Per riparare serve ffmpeg. Su Debian/Ubuntu:  sudo apt install ffmpeg")
        return 1
    try:
        nomi = sorted(n for n in os.listdir(cartella) if n.lower().endswith(".mp3") and not n.startswith("."))
    except OSError:
        nomi = []
    if not nomi:
        print("Nessun mp3 in questa cartella.")
        return 0
    pannello = Pannello(f"BLACKOUT2PODCAST {__version__}  RIPARA")
    av, sistema, riparati, errori = Avanzamento(len(nomi)), Sistema(), 0, 0
    av.dettaglio = "confronto la durata dichiarata con quella vera"
    try:
        for posizione, nome in enumerate(nomi, 1):
            percorso = os.path.join(cartella, nome)
            av.etichetta = f"CONTROLLO {posizione}/{len(nomi)}  {pulisci(nome)}"
            pannello.mostra(righe_stato(av, sistema, cartella))
            valori = durate(percorso)
            if valori and da_riparare(*valori):
                try:
                    rimuxa(percorso)
                    riparati += 1
                    pannello.messaggio(f"  RIPARATO  {nome[:45]}  "
                                       f"{formato_tempo(valori[0])} -> {formato_tempo(valori[1])}")
                except (OSError, subprocess.SubprocessError, mutagen.MutagenError) as errore:
                    errori += 1
                    pannello.messaggio(f"  ERR  {nome[:45]}: {pulisci(errore)[:60]}")
            av.fatte += 1
        pannello.mostra(righe_stato(av, sistema, cartella), subito=True)
        pannello.chiudi()
    except KeyboardInterrupt:
        pannello.chiudi()
        print("\nINTERROTTO.")
        errori += 1
    print(f"\nControllati {av.fatte} file: {riparati} riparati, {errori} errori.")
    return errori


# ============================================================================
#  INTERFACCIA INTERATTIVA
# ============================================================================

def leggi_riga(prompt, massimo=300):
    """input() con i controlli: niente caratteri di controllo, lunghezza
    massima. Ritorna None se il testo non va bene."""
    try:
        testo = input(prompt)
    except EOFError:
        print()
        raise SystemExit(0) from None
    testo = testo.strip()
    if len(testo) > massimo or CONTROLLO.search(testo):
        print("  Caratteri non validi o testo troppo lungo.")
        return None
    return testo


def chiedi(domanda, predefinito="", valida=None, vuoto_ok=False):
    """Chiede un testo finche' non e' valido. INVIO conferma il valore tra []."""
    suggerimento = f" [{predefinito}]" if predefinito else ""
    while True:
        testo = leggi_riga(f"{domanda}{suggerimento}: ")
        if testo is None:
            continue
        testo = testo or predefinito
        if not testo and not vuoto_ok:
            print("  Serve un valore.")
            continue
        errore = valida(testo) if (valida and testo) else None
        if errore:
            print(f"  {errore}")
            continue
        return testo


def si_no(domanda):
    """Domanda S/N. Ritorna True per S."""
    while True:
        risposta = (leggi_riga(f"{domanda} (S/N)? ") or "").upper()
        if risposta in ("S", "N"):
            return risposta == "S"


def pausa():
    leggi_riga("\nPremere INVIO per continuare . . .")


def schermata_avvio():
    """Titolo, radio e controllo degli strumenti, come un vecchio DOS."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[2J\x1b[H")          # CLS
    print("C:\\> BLACKOUT2PODCAST\n")
    print(f"BLACKOUT2PODCAST  Versione {__version__}")
    print("Pubblico dominio  -  The Unlicense")
    print(RADIO)
    print(f"Controllo requests ......... OK  {requests.__version__}")
    print(f"Controllo mutagen .......... OK  {mutagen.version_string}")
    if FFMPEG and FFPROBE:
        print("Controllo ffmpeg ........... OK")
    else:
        print("Controllo ffmpeg ........... MANCA")
        print("  Senza ffmpeg salto le puntate in piu' pezzi e non posso riparare.")
    print()


def scegli_show(sessione):
    """Cerca e sceglie una trasmissione. Ritorna (slug, titolo)."""
    print("Leggo l'elenco delle trasmissioni ...")
    try:
        tutte = elenco_show(sessione)
        print(f"{len(tutte)} trasmissioni disponibili.\n")
    except ErroreRete as errore:
        print(f"Elenco non disponibile ({pulisci(errore)}). Puoi scrivere lo slug o incollare il link.\n")
        tutte = []
    while True:
        cerca = chiedi("Cerca (parte del nome o link, INVIO = tutte)", vuoto_ok=True)
        diretto = slug_da_testo(cerca) if cerca else None
        if cerca.startswith(("http://", "https://")) or (diretto and not tutte):
            if diretto:
                return diretto, dict(tutte).get(diretto, titolo_da_slug(diretto))
            print("  Link non valido: serve https://radioblackout.org/shows/<nome>")
            continue
        chiave = cerca.lower()
        trovate = [s for s in tutte if chiave in s[1].lower() or chiave in s[0]]
        if not trovate:
            print("  Nessuna trasmissione trovata.")
            continue
        mostrate = trovate[:20]
        for i, (_, titolo) in enumerate(mostrate, 1):
            print(f"  {i:2d}. {titolo}")
        if len(trovate) > 20:
            print(f"  ... altre {len(trovate) - 20}: restringi la ricerca.")

        def valida_numero(testo, mostrate=mostrate):
            return None if testo.isdigit() and 1 <= int(testo) <= len(mostrate) else "Numero non valido"

        scelta = chiedi("Numero (INVIO = nuova ricerca)", valida=valida_numero, vuoto_ok=True)
        if scelta:
            return mostrate[int(scelta) - 1]


def scegli_cartella(config):
    """Chiede la cartella base e la ricorda. Ritorna il percorso o None."""
    while True:
        testo = chiedi("Cartella base dei podcast", config.get("cartella", ""), valida=valida_cartella)
        cartella = os.path.abspath(os.path.expanduser(testo))
        if not os.path.isdir(cartella):
            if not si_no(f"La cartella {cartella} non esiste. La creo"):
                continue
            try:
                os.makedirs(cartella, exist_ok=True)
            except OSError as errore:
                print(f"  Impossibile crearla: {errore}")
                continue
        if not os.access(cartella, os.W_OK):
            print("  Non posso scrivere in questa cartella.")
            continue
        config["cartella"] = cartella
        salva_config(config)
        return cartella


def menu_feed(config, cartella):
    """Chiede l'indirizzo pubblico e scrive feed.xml."""
    if not os.path.isdir(cartella):
        print("Prima scarica qualche puntata.")
        return
    print("\nIl feed punta agli mp3 serviti dal TUO server web (nginx, Caddy...).")
    print("Indica l'indirizzo pubblico della cartella base: aggiungo io il nome dello show.")
    base = chiedi("Indirizzo pubblico", config.get("feed_url", ""), valida=valida_url_base)
    config["feed_url"] = base
    salva_config(config)
    titolo = chiedi("Titolo del podcast", config.get("titolo_show") or titolo_da_slug(config["show"]))
    url_cartella = base.rstrip("/") + "/" + quote(config["show"]) + "/"
    quante = crea_feed(cartella, url_cartella, titolo, f"{SITO}/shows/{config['show']}")
    print(f"\nFEED PRONTO: {quante} puntate.")
    print(f"Indirizzo da dare alle app:  {url_cartella}feed.xml")
    if not any(os.path.isfile(os.path.join(cartella, c)) for c in ("cover.jpg", "cover.jpeg", "cover.png")):
        print("Suggerimento: metti un'immagine cover.jpg nella cartella per avere la copertina.")


MENU = """
  1  SCARICA       tutte le puntate che mancano
  2  AGGIORNA      solo le puntate uscite dopo l'ultima che hai
  3  RIPARA        sistema le puntate che si interrompono
  4  FEED          crea il feed per le app di podcast
  5  TRASMISSIONE  scegli un'altra trasmissione
  6  CARTELLA      cambia la cartella base
  0  ESCI
"""


def interattivo():
    config = carica_config()
    schermata_avvio()
    sessione = nuova_sessione()
    if not config.get("show") or not slug_da_testo(config["show"]):
        scelta = scegli_show(sessione)
        config["show"], config["titolo_show"] = scelta
        salva_config(config)
    if not config.get("cartella") or valida_cartella(config["cartella"]):
        scegli_cartella(config)

    while True:
        cartella = os.path.join(config["cartella"], config["show"])
        titolo = config.get("titolo_show") or titolo_da_slug(config["show"])
        print("\n" + "\n".join(riquadro([f"TRASMISSIONE  {titolo}", f"CARTELLA      {cartella}"],
                                        f"BLACKOUT2PODCAST {__version__}")))
        print(MENU)
        scelta = leggi_riga("C:\\BLACKOUT> ")
        if scelta is None:
            continue
        try:
            if scelta == "1":
                scarica(config["show"], titolo, cartella)
            elif scelta == "2":
                scarica(config["show"], titolo, cartella, solo_nuove=True)
            elif scelta == "3":
                ripara(cartella)
            elif scelta == "4":
                menu_feed(config, cartella)
            elif scelta == "5":
                config["show"], config["titolo_show"] = scegli_show(sessione)
                salva_config(config)
                continue
            elif scelta == "6":
                scegli_cartella(config)
                continue
            elif scelta == "0":
                print("\nArrivederci.")
                return
            else:
                print("Comando o nome file errato")
                continue
        except ErroreRete as errore:
            print(f"\nERRORE DI RETE: {pulisci(errore)}")
        pausa()


# ============================================================================
#  MODALITA' AUTOMATICA (per cron) E AVVIO
# ============================================================================

def leggi_opzioni(argv):
    p = argparse.ArgumentParser(
        prog="blackout2podcast", add_help=False,
        description="Scarica le trasmissioni di Radio Blackout e ne fa un podcast. "
                    "Senza opzioni parte il menu interattivo.")
    p.add_argument("-h", "--aiuto", action="help", help="mostra questo aiuto")
    p.add_argument("--versione", action="version", version=f"blackout2podcast {__version__}")
    p.add_argument("--show", help="trasmissione: slug o link radioblackout.org/shows/...")
    p.add_argument("--cartella", help="cartella base (default: l'ultima usata)")
    p.add_argument("--modo", choices=("scarica", "aggiorna", "ripara", "feed"), default="aggiorna",
                   help="cosa fare (default: aggiorna)")
    p.add_argument("--feed-url", help="indirizzo pubblico della cartella base: crea anche il feed")
    p.add_argument("--titolo", help="titolo del podcast nel feed")
    return p.parse_args(argv)


def automatico(opzioni):
    """Esegue un'azione senza domande. Ritorna il codice d'uscita."""
    config = carica_config()
    slug = slug_da_testo(opzioni.show)
    if not slug:
        print("Trasmissione non valida: usa lo slug o il link radioblackout.org/shows/...")
        return 2
    base = opzioni.cartella or config.get("cartella", "")
    if not base or valida_cartella(base):
        print("Cartella non valida: indica --cartella con un percorso completo.")
        return 2
    cartella = os.path.join(os.path.abspath(os.path.expanduser(base)), slug)
    titolo = opzioni.titolo or titolo_da_slug(slug)
    errori = 0
    try:
        if opzioni.modo in ("scarica", "aggiorna"):
            errori = scarica(slug, titolo, cartella, solo_nuove=opzioni.modo == "aggiorna")
        elif opzioni.modo == "ripara":
            errori = ripara(cartella)
    except ErroreRete as errore:
        print(f"ERRORE DI RETE: {pulisci(errore)}")
        return 1
    if opzioni.feed_url or opzioni.modo == "feed":
        url = opzioni.feed_url or config.get("feed_url", "")
        if not url or valida_url_base(url):
            print("Per il feed serve --feed-url con un indirizzo https:// valido.")
            return 2
        if not os.path.isdir(cartella):
            print(f"Cartella inesistente: {cartella}")
            return 2
        url_cartella = url.rstrip("/") + "/" + quote(slug) + "/"
        quante = crea_feed(cartella, url_cartella, titolo, f"{SITO}/shows/{slug}")
        print(f"Feed aggiornato: {quante} puntate -> {url_cartella}feed.xml")
    return 1 if errori else 0


def principale(argv=None):
    opzioni = leggi_opzioni(argv)
    if opzioni.show:
        return automatico(opzioni)
    try:
        interattivo()
    except KeyboardInterrupt:
        print("\n\nArrivederci.")
    return 0


if __name__ == "__main__":
    sys.exit(principale())
