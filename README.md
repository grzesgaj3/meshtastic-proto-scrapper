# meshtastic-proto-scrapper

Mostek między radiem Meshtastic podłączonym po serialu a plikami, które udają API.

Skrypt działa w jednej pętli i robi dwie rzeczy:

1. **Mesh → pliki** — nasłuchuje tekstowych wiadomości z mesha. Każdą odebraną
   wiadomość zapisuje w `messages.jsonl`, a dodatkowo te w formacie
   `sensor1: <przechylenia>;<mm>` trafiają jako odczyt do `readings.jsonl`.
   Czas bierze z metadanych pakietu (`rxTime`), nie z treści.
2. **`commands.json` → mesh** — co kilka sekund sprawdza plik z komendami i dla
   każdej nowej komendy wysyła na kanał główny `sensor1;<komenda>\n`.

Protobuf obsługuje biblioteka `meshtastic` — skrypt dostaje już zdekodowany
pakiet i nie dotyka go ręcznie.

## Instalacja

Na Raspberry Pi:

```bash
sudo apt install python3-venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Dostęp do portu szeregowego:

```bash
sudo usermod -aG dialout "$USER"
```

Po dodaniu do grupy wyloguj się i zaloguj ponownie. Port sprawdzisz tak:

```bash
ls /dev/ttyUSB* /dev/ttyACM*
```

## Uruchomienie

```bash
.venv/bin/python scrapper.py --port /dev/ttyACM0
```

Bez argumentów skrypt użyje `/dev/ttyUSB0`. Dostępne opcje (każda ma też
odpowiednik w zmiennej środowiskowej):

| Opcja | Zmienna | Domyślnie |
|---|---|---|
| `--port` | `MESHTASTIC_PORT` | `/dev/ttyUSB0` |
| `--channel` | `MESHTASTIC_CHANNEL` | `0` (kanał główny) |
| `--sensor` | `SENSOR_NAME` | `sensor1` |
| `--poll-interval` | `POLL_INTERVAL` | `5` s |
| `--reconnect-delay` | `RECONNECT_DELAY` | `5` s |
| `--readings-file` | `READINGS_FILE` | `readings.jsonl` |
| `--messages-file` | `MESSAGES_FILE` | `messages.jsonl` |
| `--commands-file` | `COMMANDS_FILE` | `commands.json` |
| `--state-file` | `STATE_FILE` | `state.json` |

Pliki domyślnie leżą obok `scrapper.py`. Zatrzymanie: `Ctrl+C`. Po zniknięciu
radia skrypt próbuje połączyć się ponownie.

## Format danych

### Wszystkie wiadomości — `messages.jsonl`

Każda odebrana wiadomość tekstowa, niezależnie od treści:

```json
{"timestamp": "2026-10-30T12:34:56Z", "from": "!28b54624", "channel": 0, "packet_id": 589440320, "text": "sensor1;123"}
```

`from` to ID węzła (`fromId`), a gdy go brakuje — numer węzła. Duplikaty tego
samego pakietu są pomijane.

### Odczyty — `readings.jsonl`

Tylko wiadomości pasujące do `sensor1: <liczba>;<mm>`. Jedna linia = jedno
symulowane ciało POST-a:

```json
{"timestamp": "2026-10-30T12:34:56Z", "sensor": "sensor1", "tilts": 1234, "rain_mm": 20.0}
```

`timestamp` pochodzi z `rxTime` pakietu (zegar radia odbiorczego). Jeśli pakiet
go nie ma, używany jest zegar Raspberry Pi.

### Komendy — `commands.json`

```json
[
  {"id": 1, "command": "reset"},
  {"id": 2, "command": "measure"}
]
```

Każda komenda musi mieć unikalne `id` (liczba lub string). Po wysłaniu `id`
trafia do `state.json`, więc restart nie wyśle jej drugi raz. Aby powtórzyć tę
samą komendę, nadaj jej nowe `id`.

## Testy

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Podmiana plików na prawdziwe API

Miejsca do zmiany:

- `append_record()` — zamiast dopisywać linię, zrób `POST` z rekordem
  (dotyczy zarówno `messages.jsonl`, jak i `readings.jsonl`).
- `load_commands()` — zamiast czytać plik, pobierz komendy z API.
- `save_state()` / `load_state()` — oznaczanie komend jako wysłanych po stronie API.

Reszta logiki (parsowanie, deduplikacja, reconnect) zostaje bez zmian.
