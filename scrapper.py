"""Mostek między Meshtastic (serial/protobuf) a API RainGauge."""

import json
import logging
import os
import re
import threading
import time
import urllib.request
from argparse import ArgumentParser
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

READING_RE = re.compile(r"^\s*([^:;]+?)\s*:\s*(\d+)\s*;\s*(\d+(?:\.\d+)?)\s*$")

API_TIMEOUT = 5
CLEAN_COMMAND = "c"

log = logging.getLogger("scrapper")


def parse_reading(text):
    """Parse ``sensor1: 1234;20`` into a dict with sensor name, tilts and mm."""
    match = READING_RE.match(text or "")
    if match is None:
        return None
    return {
        "sensor": match.group(1),
        "tilts": int(match.group(2)),
        "rain_mm": float(match.group(3)),
    }


def iso_utc(epoch):
    """Format an epoch timestamp as ISO 8601 in UTC."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_record(text, rx_time=None, now=None):
    """Build the simulated POST body from a mesh text message and packet time."""
    reading = parse_reading(text)
    if reading is None:
        return None
    timestamp = rx_time if rx_time else (now or time.time)()
    return {"timestamp": iso_utc(int(timestamp)), **reading}


def build_message(packet, text):
    """Build a raw record for every text message received from the mesh."""
    timestamp = packet.get("rxTime") or time.time()
    return {
        "timestamp": iso_utc(int(timestamp)),
        "from": packet.get("fromId", packet.get("from")),
        "channel": packet.get("channel", 0),
        "packet_id": packet.get("id"),
        "text": text,
    }


def append_record(path, record):
    """Append one JSON record as a line, flushing it to disk immediately."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def format_command(sensor, command):
    """Build the text sent to the mesh, e.g. ``sensor1;reset\\n``."""
    return f"{sensor};{command}\n"


def api_get_json(url, *, opener=urllib.request.urlopen, timeout=API_TIMEOUT):
    """Fetch and decode one JSON response from the API."""
    with opener(url, timeout=timeout) as response:
        return json.load(response)


def upload_reading(api_url, record, *, opener=urllib.request.urlopen, timeout=API_TIMEOUT):
    """POST one reading to the API; failures are logged, not raised."""
    body = json.dumps(
        {
            "millimeters": record["rain_mm"],
            "deviceTimestamp": record["timestamp"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/Device/upload",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            response.read()
    except OSError as exc:
        log.warning("Nie mogę wysłać odczytu do API: %s", exc)
        return False
    log.info("Wysłano odczyt do API: %.1f mm", record["rain_mm"])
    return True


def poll_commands(radio, api_url, sensor, channel=0, *, opener=urllib.request.urlopen):
    """Send the cleaning command when the API reports one as pending."""
    base = api_url.rstrip("/")
    try:
        status = api_get_json(f"{base}/api/Device/clean-status", opener=opener)
    except (OSError, ValueError) as exc:
        log.warning("Nie mogę odczytać statusu czyszczenia: %s", exc)
        return 0
    if not isinstance(status, dict) or not status.get("isPending"):
        return 0
    try:
        radio.sendText(format_command(sensor, CLEAN_COMMAND), channelIndex=channel)
    except Exception as exc:  # noqa: BLE001 - radio failures must not kill polling
        log.warning("Nie mogę wysłać komendy czyszczenia: %s", exc)
        return 0
    try:
        api_get_json(f"{base}/api/Device/commands", opener=opener)
    except (OSError, ValueError) as exc:
        log.warning("Nie mogę potwierdzić komendy czyszczenia: %s", exc)
    log.info("Wysłano komendę czyszczenia %r", CLEAN_COMMAND)
    return 1


class SeenPackets:
    """Remember recently handled packet ids to drop mesh duplicates."""

    def __init__(self, maxlen=5000):
        self._order = deque(maxlen=maxlen)
        self._keys = set()

    def add(self, key):
        """Return False when the packet was already seen."""
        if key in self._keys:
            return False
        if len(self._order) == self._order.maxlen:
            self._keys.discard(self._order.popleft())
        self._order.append(key)
        self._keys.add(key)
        return True


def on_receive(packet, interface=None, *, readings_path, messages_path, seen, api_url=None):
    """Pubsub handler: save every mesh text and also parse sensor readings."""
    decoded = packet.get("decoded")
    text = decoded.get("text") if isinstance(decoded, dict) else None
    if not text:
        return

    key = (packet.get("from"), packet.get("id"))
    if key[1] is not None and not seen.add(key):
        log.debug("Pomijam duplikat pakietu id=%s", key[1])
        return

    append_record(messages_path, build_message(packet, text))

    record = build_record(text, packet.get("rxTime"))
    if record is None:
        log.debug("Zapisano wiadomość bez odczytu: %r", text)
        return

    append_record(readings_path, record)
    if api_url:
        upload_reading(api_url, record)
    log.info(
        "Odczyt: %s przechyleń, %.1f mm (pakiet id=%s)",
        record["tilts"],
        record["rain_mm"],
        key[1],
    )


def make_handler(readings_path, messages_path, seen, api_url=None):
    """Wrap :func:`on_receive` in a plain two-arg pubsub listener."""

    def handler(packet, interface=None):
        try:
            on_receive(
                packet,
                interface,
                readings_path=readings_path,
                messages_path=messages_path,
                seen=seen,
                api_url=api_url,
            )
        except Exception:  # noqa: BLE001 - never kill the radio reader thread
            log.exception("Błąd przetwarzania pakietu")

    return handler


def is_connected(interface):
    """Meshtastic exposes ``isConnected`` as a threading.Event, not a bool."""
    state = getattr(interface, "isConnected", False)
    return state.is_set() if hasattr(state, "is_set") else bool(state)


def serve(interface, *, api_url, sensor, channel, poll_interval, stop_event):
    """Poll the API for commands while the radio stays connected."""
    while is_connected(interface) and not stop_event.is_set():
        poll_commands(interface, api_url, sensor, channel)
        stop_event.wait(poll_interval)


BASE_DIR = Path(__file__).resolve().parent


def parse_args(argv=None):
    parser = ArgumentParser(description="Mostek Meshtastic <-> API RainGauge")
    parser.add_argument(
        "--port",
        default=os.environ.get("MESHTASTIC_PORT", "/dev/ttyUSB0"),
        help="port szeregowy radia (domyślnie /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--channel",
        type=int,
        default=int(os.environ.get("MESHTASTIC_CHANNEL", "0")),
        help="indeks kanału do wysyłki (domyślnie 0, kanał główny)",
    )
    parser.add_argument(
        "--sensor",
        default=os.environ.get("SENSOR_NAME", "sensor1"),
        help="nazwa sensora w wysyłanych komendach",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("API_URL", "http://127.0.0.1:8080"),
        help="adres API deszczomierza (domyślnie http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("POLL_INTERVAL", "5")),
        help="co ile sekund pytać API o komendy",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=float(os.environ.get("RECONNECT_DELAY", "5")),
        help="co ile sekund ponawiać połączenie z radiem",
    )
    parser.add_argument(
        "--readings-file",
        type=Path,
        default=Path(os.environ.get("READINGS_FILE", BASE_DIR / "readings.jsonl")),
        help="plik, do którego dopisujemy odczyty (JSONL)",
    )
    parser.add_argument(
        "--messages-file",
        type=Path,
        default=Path(os.environ.get("MESSAGES_FILE", BASE_DIR / "messages.jsonl")),
        help="plik, do którego zapisujemy każdą odebraną wiadomość (JSONL)",
    )
    return parser.parse_args(argv)


def run(args):
    """Connect to the radio forever, reconnecting when it disappears."""
    from meshtastic.serial_interface import SerialInterface
    from pubsub import pub

    seen = SeenPackets()
    stop_event = threading.Event()
    handler = make_handler(args.readings_file, args.messages_file, seen, args.api_url)
    pub.subscribe(handler, "meshtastic.receive.text")

    log.info(
        "Start: port=%s kanał=%d sensor=%s api=%s odczyty=%s wiadomości=%s",
        args.port,
        args.channel,
        args.sensor,
        args.api_url,
        args.readings_file,
        args.messages_file,
    )
    try:
        while not stop_event.is_set():
            interface = None
            try:
                interface = SerialInterface(devPath=args.port)
                log.info("Połączono z radiem")
                serve(
                    interface,
                    api_url=args.api_url,
                    sensor=args.sensor,
                    channel=args.channel,
                    poll_interval=args.poll_interval,
                    stop_event=stop_event,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - radio may fail in many ways
                log.warning("Brak łączności z radiem: %s", exc)
            finally:
                if interface is not None:
                    try:
                        interface.close()
                    except Exception:  # noqa: BLE001 - closing must never crash us
                        pass
            if stop_event.wait(args.reconnect_delay):
                break
    except KeyboardInterrupt:
        log.info("Przerwano przez użytkownika")
    finally:
        try:
            pub.unsubscribe(handler, "meshtastic.receive.text")
        except Exception:  # noqa: BLE001
            pass


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run(parse_args(argv))


if __name__ == "__main__":
    main()
