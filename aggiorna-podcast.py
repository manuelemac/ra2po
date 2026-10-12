#!/usr/bin/env python3
"""Aggiornamento automatico (da cron) dei podcast scaricati con blackout2podcast.

Per ogni show scarica tutte le puntate uscite sul sito dopo l'ultima presente
in locale (confronto per data; a parita' di data, per titolo) e le numera di
seguito all'ultimo numero presente. Uno show che fallisce non blocca gli altri;
se una puntata fallisce, lo show si ferma li' e riprende al giro dopo.
Se configurato, alla fine chiede ad Audiobookshelf di rileggere i podcast
aggiornati.

Download/unione/tag: funzioni di blackout2podcast.py (stessa cartella).
Configurazione: ~/.config/ra2po/config.json (vedi config.example.json),
oppure il percorso passato con --config.
Uso: aggiorna-podcast.py [--prova] [--config FILE]
     (--prova: controlla senza scaricare)
"""
import argparse
import json
import re
import shutil
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blackout2podcast as b  # noqa: E402

CONFIG = Path.home() / ".config/ra2po/config.json"
BASE = ABS = TOKEN = None
PROVA = False
NUMERO = re.compile(r"^(\d+) - (\d{4}-\d{2}-\d{2}) - ")


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def carica_config(percorso):
    """Legge la configurazione. Ritorna {cartella: slug} degli show."""
    global BASE, ABS, TOKEN
    config = json.loads(Path(percorso).expanduser().read_text(encoding="utf-8"))
    BASE = Path(config["cartella"]).expanduser()
    abs_conf = config.get("audiobookshelf") or {}
    if abs_conf.get("url"):
        ABS = abs_conf["url"].rstrip("/")
        TOKEN = Path(abs_conf["token_file"]).expanduser().read_text().strip()
    return config["show"]


def abs_api(path, method="GET"):
    req = urllib.request.Request(ABS + path, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    return json.loads(body) if body.strip().startswith((b"{", b"[")) else None


def stato_locale(cartella):
    """Puntate presenti (anche nelle sottocartelle): (numero piu' alto,
    data piu' recente, nomi senza numero, cartella dell'ultima puntata)."""
    numero, data, nomi, dove = 0, "", set(), cartella
    for f in cartella.rglob("*.mp3"):
        m = NUMERO.match(f.name)
        if not m or any(d.startswith(".tmp") for d in f.parts):
            continue
        nomi.add(b.PREFISSO.sub("", f.name))
        data = max(data, m.group(2))
        if int(m.group(1)) > numero:
            numero, dove = int(m.group(1)), f.parent
    return numero, data, nomi, dove


def nuove_dal_sito(sessione, slug, dal_giorno, presenti):
    """Puntate con audio uscite dal giorno dell'ultima presente in poi e non
    ancora scaricate, dalla piu' vecchia. Legge le pagine dalla piu' recente e
    si ferma appena arriva a date gia' coperte."""
    for tassonomia in ("podcastfilter", "tags"):
        dati, _ = b.leggi_json(sessione, tassonomia, {"slug": slug, "_fields": "id"})
        if dati:
            break
    else:
        raise b.ErroreRete(f"show '{slug}' non trovato sul sito")
    nuove, pagina, pagine = [], 1, 1
    while pagina <= pagine:
        voci, pagine = b.leggi_json(sessione, "podcast", {
            tassonomia: dati[0]["id"], "per_page": 30, "page": pagina,
            "_fields": "link,date,title,content", "orderby": "date", "order": "desc"})
        puntate = [b.estrai_puntata(v) for v in voci if isinstance(v, dict)]
        # stesso giorno dell'ultima: la prendo solo se non c'e' gia' (es. 3 puntate nello stesso giorno)
        nuove += [p for p in puntate if p["mp3s"] and p["date"] >= dal_giorno
                  and b.PREFISSO.sub("", b.nome_file(1, p)) not in presenti]
        if not puntate or puntate[-1]["date"] < dal_giorno:
            break
        pagina += 1
        time.sleep(b.PAUSA)
    return nuove[::-1]


def aggiorna_show(sessione, nome, slug, titolo):
    if in_uso(BASE / nome):
        log(f"{titolo}: download in corso con blackout2podcast, salto")
        return False
    numero, dal_giorno, presenti, cartella = stato_locale(BASE / nome)
    if not dal_giorno:
        log(f"{titolo}: nessuna puntata in locale, salto (primo download con blackout2podcast.py)")
        return False
    nuove = nuove_dal_sito(sessione, slug, dal_giorno, presenti)
    if not nuove:
        log(f"{titolo}: in pari (ultima #{numero} del {dal_giorno})")
        return False
    log(f"{titolo}: {len(nuove)} nuove dopo #{numero} del {dal_giorno}")
    tmp = cartella / ".tmp-cron"         # non .tmp: e' di blackout2podcast
    fatte = 0
    for puntata in nuove:
        numero += 1                 # numerazione sequenziale, di seguito all'ultima presente
        nome_mp3 = b.nome_file(numero, puntata)
        if PROVA:
            log(f"{titolo}: [prova] scaricherei {cartella.relative_to(BASE)}/{nome_mp3}")
            continue
        tmp.mkdir(exist_ok=True)
        try:
            b.scarica_puntata(sessione, puntata, numero=numero, finale=str(cartella / nome_mp3),
                              temporanei=str(tmp), album=titolo,
                              av=b.Avanzamento(1), aggiorna=lambda: None)
        except Exception:
            # mi fermo qui: al prossimo giro riparte da questa, senza buchi nei numeri
            shutil.rmtree(tmp, ignore_errors=True)
            if fatte:
                abbina_manifest(cartella, nuove[:fatte])
            raise
        fatte += 1
        log(f"{titolo}: scaricata #{numero} {puntata['date']} {puntata['title']}")
    shutil.rmtree(tmp, ignore_errors=True)
    if fatte:
        abbina_manifest(cartella, nuove[:fatte])
    return fatte > 0


def in_uso(cartella, minuti=60):
    """True se blackout2podcast sta scaricando qui (file .part recenti in .tmp)."""
    limite = time.time() - minuti * 60
    return any(f.stat().st_mtime > limite for f in cartella.rglob(".tmp/*"))


def abbina_manifest(cartella, scaricate):
    """Aggiunge le puntate scaricate a episodi.json, se la cartella lo usa."""
    manifest = cartella / b.MANIFEST
    if manifest.exists():
        elenco = json.loads(manifest.read_text(encoding="utf-8"))
        visti = {(p.get("date"), p.get("title")) for p in elenco}
        elenco += [p for p in scaricate if (p["date"], p["title"]) not in visti]
        b.scrivi_json(str(manifest), elenco)


def main():
    global PROVA
    p = argparse.ArgumentParser(description="Scarica le puntate nuove dei podcast configurati.")
    p.add_argument("--prova", action="store_true", help="controlla senza scaricare")
    p.add_argument("--config", default=str(CONFIG), help=f"file di configurazione (default {CONFIG})")
    opzioni = p.parse_args()
    PROVA = opzioni.prova
    show = carica_config(opzioni.config)

    # cartella -> (id item ABS, titolo) dalle librerie podcast di Audiobookshelf
    items = {}
    if ABS:
        for lib in abs_api("/api/libraries")["libraries"]:
            if lib["mediaType"] == "podcast":
                for it in abs_api(f"/api/libraries/{lib['id']}/items?limit=0")["results"]:
                    items[Path(it["path"]).name] = (it["id"], it["media"]["metadata"]["title"])

    sessione, errori = b.nuova_sessione(), 0
    for nome, slug in show.items():
        item_id, titolo = items.get(nome, (None, b.titolo_da_slug(slug)))
        try:
            if aggiorna_show(sessione, nome, slug, titolo) and ABS:
                if item_id:
                    abs_api(f"/api/items/{item_id}/scan", "POST")
                else:
                    log(f"{titolo}: non trovato in ABS, rilancia la scansione della libreria")
        except Exception as e:      # un show che fallisce non blocca gli altri
            errori += 1
            log(f"{titolo}: ERRORE {type(e).__name__}: {b.pulisci(str(e))[:200]}")
    return 1 if errori else 0


if __name__ == "__main__":
    sys.exit(main())
