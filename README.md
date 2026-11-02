# meshtastic-proto-scrapper

Mostek między radiem Meshtastic podłączonym po serialu a API deszczomierza
(`RainGaugeServer`).

Skrypt działa w jednej pętli i robi dwie rzeczy:

1. **Mesh → pliki i API** — nasłuchuje tekstowych wiadomości z mesha. Każdą
   odebraną wiadomość zapisuje w `messages.jsonl`, a dodatkowo te w formacie
   `sensor1;<mm>` lub `sensor1: <przechylenia>;<mm>` trafiają jako odczyt do
   `readings.jsonl` oraz przez `POST /api/Device/upload` do API. Czas bierze
   z metadanych pakietu (`rxTime`), nie z treści.
2. **API → mesh** — co kilka sekund pyta `GET /api/Device/clean-status`.
   Gdy czyszczenie jest oczekujące, wysyła na kanał główny `sensor1;c\n`,
   a po udanej wysyłce odbiera komendę przez `GET /api/Device/commands`.

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

API musi być osiągalne pod `http://127.0.0.1:8080`; inny adres podaj przez
`--api-url` (np. `--api-url http://192.168.1.10:8080`).
Bez argumentów skrypt użyje `/dev/ttyUSB0`. Dostępne opcje (każda ma też
odpowiednik w zmiennej środowiskowej):

| Opcja | Zmienna | Domyślnie |
|---|---|---|
| `--port` | `MESHTASTIC_PORT` | `/dev/ttyUSB0` |
| `--channel` | `MESHTASTIC_CHANNEL` | `0` (kanał główny) |
| `--sensor` | `SENSOR_NAME` | `sensor1` |
| `--api-url` | `API_URL` | `http://127.0.0.1:8080` |
| `--poll-interval` | `POLL_INTERVAL` | `5` s |
| `--reconnect-delay` | `RECONNECT_DELAY` | `5` s |
| `--readings-file` | `READINGS_FILE` | `readings.jsonl` |
| `--messages-file` | `MESSAGES_FILE` | `messages.jsonl` |

Pliki domyślnie leżą obok `scrapper.py`. Zatrzymanie: `Ctrl+C`. Po zniknięciu
radia skrypt próbuje połączyć się ponownie.

## Format danych

### Wszystkie wiadomości — `messages.jsonl`

Każda odebrana wiadomość tekstowa, niezależnie od treści:

```json
{"timestamp": "2026-10-30T12:34:56Z", "from": "!28b54624", "channel": 0, "packet_id": 589440320, "text": "sensor1;9"}
```

`from` to ID węzła (`fromId`), a gdy go brakuje — numer węzła. Duplikaty tego
samego pakietu są pomijane.

### Odczyty — `readings.jsonl`

Akceptowane są dwa formaty:

- `sensor1;9` — tylko milimetry (tak wysyła urządzenie),
- `sensor1: 1234;20` — liczba przechyleń i milimetry (starszy format).

Jedna linia = jeden odczyt, który jest też wysyłany do API:

```json
{"timestamp": "2026-10-30T12:34:56Z", "sensor": "sensor1", "tilts": null, "rain_mm": 9.0}
{"timestamp": "2026-10-30T12:34:56Z", "sensor": "sensor1", "tilts": 1234, "rain_mm": 20.0}
```

Dla formatu bez przechyleń `tilts` ma wartość `null`. Do API trafia tylko
`rain_mm` jako `millimeters`.

`timestamp` pochodzi z `rxTime` pakietu (zegar radia odbiorczego). Jeśli pakiet
go nie ma, używany jest zegar Raspberry Pi.

### Czyszczenie — API

Sterowanie odbywa się przez API deszczomierza:

- `GET /api/Device/clean-status` zwraca `{"isPending": true}`, gdy czyszczenie
  czeka na urządzenie.
- Po wysłaniu `sensor1;c` skrypt woła `GET /api/Device/commands`, co zeruje
  flagę po stronie serwera.
- Jeśli wysłanie przez mesh się nie uda, flaga zostaje i skrypt ponowi próbę
  przy kolejnym odpytaniu.
- Błąd API lub sieci jest logowany i nie zatrzymuje pętli; pliki zapisują się
  dalej.

## Testy

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## API

Domyślnie `http://127.0.0.1:8080`; zmiana przez `--api-url` lub `API_URL`.

- `POST /api/Device/upload` — `{"millimeters": <mm>, "deviceTimestamp": "<ISO 8601>"}`.
- `GET /api/Device/clean-status` — podgląd flagi czyszczenia.
- `GET /api/Device/commands` — odebranie i wyzerowanie flagi czyszczenia.

`messages.jsonl` i `readings.jsonl` nadal powstają lokalnie; wysyłka do API
jest dodatkiem i jej błąd nie przerywa zapisu ani pracy radia.
