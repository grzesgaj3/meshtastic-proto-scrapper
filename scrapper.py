"""Mostek między Meshtastic (serial/protobuf) a symulowanym API w plikach."""

import json
import logging
import os
import re
import threading
import time
from argparse import ArgumentParser
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

READING_RE = re.compile(r"^\s*([^:;]+?)\s*:\s*(\d+)\s*;\s*(\d+(?:\.\d+)?)\s*$")

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


def load_commands(path):
    """Read the simulated API command queue, ignoring anything malformed."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Nie mogę odczytać %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        log.warning("%s powinien zawierać listę komend", path)
        return []
    commands = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        command_id = entry.get("id")
        if isinstance(command_id, bool) or not isinstance(command_id, (int, str)):
            continue
        if not isinstance(command, str) or not command.strip():
            continue
        commands.append({"id": command_id, "command": command.strip()})
    return commands


def pending_commands(commands, sent_ids):
    """Return commands whose id has not been sent yet."""
    sent = set(sent_ids)
    return [command for command in commands if command["id"] not in sent]


def load_state(path):
    """Load the sent-command state, falling back to an empty state."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"sent_ids": []}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Nie mogę odczytać %s: %s", path, exc)
        return {"sent_ids": []}
    sent_ids = data.get("sent_ids") if isinstance(data, dict) else None
    if not isinstance(sent_ids, list):
        return {"sent_ids": []}
    return {"sent_ids": sent_ids}


def save_state(path, state):
    """Write state atomically so a crash cannot leave it half-written."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def poll_commands(radio, commands_path, state_path, sensor, channel=0):
    """Send every not-yet-sent command from the queue and remember it."""
    commands = load_commands(commands_path)
    if not commands:
        return 0
    state = load_state(state_path)
    sent_ids = state["sent_ids"]
    sent = set(sent_ids)
    sent_count = 0
    for command in pending_commands(commands, sent):
        radio.sendText(
            format_command(sensor, command["command"]),
            channelIndex=channel,
        )
        sent.add(command["id"])
        sent_ids.append(command["id"])
        save_state(state_path, {"sent_ids": sent_ids})
        sent_count += 1
        log.info("Wysłano komendę id=%s: %r", command["id"], command["command"])
    return sent_count


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


def on_receive(packet, interface=None, *, readings_path, seen):
    """Pubsub handler: turn a received mesh text packet into a JSON line."""
    decoded = packet.get("decoded")
    text = decoded.get("text") if isinstance(decoded, dict) else None
    record = build_record(text or "", packet.get("rxTime"))
    if record is None:
        if text:
            log.debug("Pomijam wiadomość bez odczytu: %r", text)
        return

    key = (packet.get("from"), packet.get("id"))
    if key[1] is not None and not seen.add(key):
        log.debug("Pomijam duplikat pakietu id=%s", key[1])
        return

    append_record(readings_path, record)
    log.info(
        "Odczyt: %s przechyleń, %.1f mm (pakiet id=%s)",
        record["tilts"],
        record["rain_mm"],
        key[1],
    )


def make_handler(readings_path, seen):
    """Wrap :func:`on_receive` in a plain two-arg pubsub listener."""

    def handler(packet, interface=None):
        try:
            on_receive(packet, interface, readings_path=readings_path, seen=seen)
        except Exception:  # noqa: BLE001 - never kill the radio reader thread
            log.exception("Błąd przetwarzania pakietu")

    return handler


def is_connected(interface):
    """Meshtastic exposes ``isConnected`` as a threading.Event, not a bool."""
    state = getattr(interface, "isConnected", False)
    return state.is_set() if hasattr(state, "is_set") else bool(state)


def serve(interface, *, commands_path, state_path, sensor, channel, poll_interval, stop_event):
    """Poll the command queue while the radio stays connected."""
    while is_connected(interface) and not stop_event.is_set():
        poll_commands(interface, commands_path, state_path, sensor, channel)
        stop_event.wait(poll_interval)


BASE_DIR = Path(__file__).resolve().parent


def parse_args(argv=None):
    parser = ArgumentParser(description="Mostek Meshtastic <-> plikowe API")
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
        "--poll-interval",
        type=float,
        default=float(os.environ.get("POLL_INTERVAL", "5")),
        help="co ile sekund czytać plik komend",
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
        "--commands-file",
        type=Path,
        default=Path(os.environ.get("COMMANDS_FILE", BASE_DIR / "commands.json")),
        help="plik z komendami do wysłania",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ.get("STATE_FILE", BASE_DIR / "state.json")),
        help="plik z id już wysłanych komend",
    )
    return parser.parse_args(argv)


def run(args):
    """Connect to the radio forever, reconnecting when it disappears."""
    from meshtastic.serial_interface import SerialInterface
    from pubsub import pub

    seen = SeenPackets()
    stop_event = threading.Event()
    handler = make_handler(args.readings_file, seen)
    pub.subscribe(handler, "meshtastic.receive.text")

    log.info(
        "Start: port=%s kanał=%d sensor=%s odczyty=%s komendy=%s",
        args.port,
        args.channel,
        args.sensor,
        args.readings_file,
        args.commands_file,
    )
    try:
        while not stop_event.is_set():
            interface = None
            try:
                interface = SerialInterface(devPath=args.port)
                log.info("Połączono z radiem")
                serve(
                    interface,
                    commands_path=args.commands_file,
                    state_path=args.state_file,
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
