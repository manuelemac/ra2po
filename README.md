# ra2po — da Radio Blackout a podcast

Scarica le trasmissioni di [Radio Blackout](https://radioblackout.org) e ne fa
un podcast: un mp3 per puntata (i pezzi uniti in un file solo), con i tag ID3
(titolo, data, numero, descrizione) e, se vuoi, un feed RSS. Le cartelle
che produce si possono dare in pasto direttamente ad
[Audiobookshelf](https://www.audiobookshelf.org) come libreria podcast.

## Perché

Negli anni Radio Blackout ha accumulato un archivio enorme di trasmissioni:
inchieste, approfondimenti e voci che difficilmente trovano spazio altrove. È
una fonte di informazione indipendente, libera dalle logiche del giornalismo
commerciale e dalla disinformazione. Però sul sito le puntate sono sparse,
spesso divise in più file e scomode da ascoltare di fila.

ra2po nasce per rendere quell'archivio facile da ascoltare: ogni show diventa
un podcast ordinato, con le puntate numerate, da seguire su qualsiasi app.

Per ospitarlo in casa e condividerlo con chi vuoi, consiglio
[Audiobookshelf](https://www.audiobookshelf.org): un server self-hosted e open
source per podcast e audiolibri, con app per Android e iOS, che ricorda dove sei
arrivato e può pubblicare un feed RSS per ogni podcast. Così i contenuti restano
sul tuo server, senza passare dalle grandi piattaforme.

Il progetto è stato scritto in *vibe coding* 

## Gli script

| Script | A cosa serve |
|---|---|
| `blackout2podcast.py` | Il downloader. Menu interattivo, oppure senza domande con `--show`. |
| `aggiorna-podcast.py` | Per il cron: per ogni show configurato scarica le puntate uscite dopo l'ultima che hai e, se c'è, fa rileggere i podcast ad Audiobookshelf. |

## Requisiti

- Python 3.8+
- `pip install -r requirements.txt` (requests, mutagen)
- `ffmpeg` consigliato: serve per unire le puntate divise in più pezzi
  (`sudo apt install ffmpeg`)

## blackout2podcast.py

```bash
python3 blackout2podcast.py                 # menu interattivo
python3 blackout2podcast.py --show stakka-stakka --cartella ~/podcast --modo scarica
```

`--show` accetta lo slug o il link `https://radioblackout.org/shows/<slug>`.
Modi: `scarica` (tutte le puntate mancanti), `aggiorna` (solo quelle dopo la più
recente che hai), `ripara` (sistema gli mp3 con la durata sbagliata), `feed`
(crea `feed.xml`, serve `--feed-url`).

Ogni show finisce in `<cartella>/<slug>/` con file chiamati
`NNN - AAAA-MM-GG - Titolo.mp3` e l'elenco delle puntate in `episodi.json`.
Le scelte del menu sono salvate in `~/.config/blackout2podcast/config.json`.

## aggiorna-podcast.py (cron)

1. Copia `config.example.json` in `~/.config/ra2po/config.json` e compilalo:
   - `cartella`: la cartella base dei podcast
   - `show`: nome della sottocartella → slug dello show sul sito
   - `audiobookshelf` (facoltativo): URL del server e file con la chiave API
     (in Audiobookshelf: Impostazioni → API Keys). Toglilo se non usi ABS.
2. Prova senza scaricare niente:
   ```bash
   python3 aggiorna-podcast.py --prova
   ```
3. Aggiungi il cron (`crontab -e`), di notte, per esempio ogni lunedì alle 3:30:
   ```
   30 3 * * 1 flock -n /tmp/aggiorna-podcast.lock /percorso/ra2po/aggiorna-podcast.py >> ~/aggiorna-podcast.log 2>&1
   ```

Come decide cosa scaricare: guarda la data dell'ultima puntata presente nella
cartella dello show (sottocartelle comprese), legge dal sito solo le pagine più
recenti e prende tutte le puntate uscite da quel giorno in poi che non hai già
(a parità di data confronta il titolo). Le numera di seguito al numero più alto
presente. Se un download fallisce si ferma lì e riprende al giro dopo, senza
lasciare buchi nella numerazione. Se `blackout2podcast.py` sta scaricando
nella stessa cartella, quello show viene saltato.

## Nota

Sono contenuti autoprodotti di una radio libera: scaricali per uso personale,
rispetta le indicazioni e, se ti piacciono, sostienila! 

Radio Blackout è una radio autogestita, con risorse limitate: scarica con
criterio e senza sovraccaricare il suo server.

- **Scarica di notte.** Lancia i download grossi e programma il cron in orari
  notturni, quando il sito ha meno ascoltatori.
- **Una cosa alla volta.** Scarica uno show per volta, senza processi in
  parallelo. Gli script fanno già una pausa tra una richiesta e l'altra: non
  toglierla.
- **Solo quello che ascolterai.** Prima di scaricare un archivio di centinaia
  di puntate, chiediti se ti serve tutto. Per tenerti aggiornato basta il cron
  una volta alla settimana.

## Licenza

Pubblico dominio ([The Unlicense](https://unlicense.org/)): fanne quello che vuoi.
