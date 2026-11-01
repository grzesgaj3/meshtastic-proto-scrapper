import json

import scrapper


def test_parses_reading():
    assert scrapper.parse_reading("sensor1: 1234;20") == {
        "sensor": "sensor1",
        "tilts": 1234,
        "rain_mm": 20.0,
    }


def test_tolerates_whitespace_and_trailing_newline():
    assert scrapper.parse_reading("  sensor1 :  1234 ; 20.5 \n") == {
        "sensor": "sensor1",
        "tilts": 1234,
        "rain_mm": 20.5,
    }


def test_rejects_malformed_messages():
    malformed = [
        "",
        "hello",
        "sensor1: 1234",
        "sensor1: 12;",
        "sensor1: -1;2.0",
        "sensor1: 1;2;3",
        "sensor1 1;2",
    ]
    for text in malformed:
        assert scrapper.parse_reading(text) is None, text


def test_builds_record_with_time_from_mesh_packet():
    assert scrapper.build_record("sensor1: 1234;20", rx_time=1690000000) == {
        "timestamp": "2023-07-22T04:26:40Z",
        "sensor": "sensor1",
        "tilts": 1234,
        "rain_mm": 20.0,
    }


def test_build_record_falls_back_to_local_clock():
    record = scrapper.build_record("sensor1: 1;0.5", rx_time=None, now=lambda: 1690000000)
    assert record["timestamp"] == "2023-07-22T04:26:40Z"


def test_build_record_rejects_malformed_text():
    assert scrapper.build_record("hello", rx_time=1690000000) is None


def test_appends_one_json_line_per_record(tmp_path):
    path = tmp_path / "readings.jsonl"
    first = {"timestamp": "2023-07-22T04:26:40Z", "sensor": "sensor1", "tilts": 1, "rain_mm": 0.2}
    second = {"timestamp": "2023-07-22T04:26:41Z", "sensor": "sensor1", "tilts": 2, "rain_mm": 0.4}

    scrapper.append_record(path, first)
    scrapper.append_record(path, second)

    lines = path.read_text().splitlines()
    assert [json.loads(line) for line in lines] == [first, second]


def test_format_command():
    assert scrapper.format_command("sensor1", "reset") == "sensor1;reset\n"


def test_load_commands_missing_file(tmp_path):
    assert scrapper.load_commands(tmp_path / "missing.json") == []


def test_load_commands_invalid_json(tmp_path):
    path = tmp_path / "commands.json"
    path.write_text("{not json")
    assert scrapper.load_commands(path) == []


def test_load_commands_skips_invalid_entries(tmp_path):
    path = tmp_path / "commands.json"
    path.write_text(json.dumps([{"id": 1, "command": "a"}, {"id": 2}, {"command": "b"}, "x"]))
    assert scrapper.load_commands(path) == [{"id": 1, "command": "a"}]


def test_pending_commands_skips_already_sent():
    commands = [{"id": 1, "command": "a"}, {"id": 2, "command": "b"}]
    assert scrapper.pending_commands(commands, {1}) == [{"id": 2, "command": "b"}]


def test_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    assert scrapper.load_state(path) == {"sent_ids": []}
    scrapper.save_state(path, {"sent_ids": [1, 2]})
    assert scrapper.load_state(path) == {"sent_ids": [1, 2]}


class FakeRadio:
    isConnected = True

    def __init__(self):
        self.sent = []

    def sendText(self, text, channelIndex=0):
        self.sent.append((text, channelIndex))


def test_poll_commands_sends_unsent_commands_once(tmp_path):
    commands_path = tmp_path / "commands.json"
    state_path = tmp_path / "state.json"
    commands_path.write_text(
        json.dumps([{"id": 1, "command": "reset"}, {"id": 2, "command": "measure"}])
    )
    radio = FakeRadio()

    assert scrapper.poll_commands(radio, commands_path, state_path, "sensor1") == 2
    assert radio.sent == [("sensor1;reset\n", 0), ("sensor1;measure\n", 0)]
    assert scrapper.load_state(state_path) == {"sent_ids": [1, 2]}

    assert scrapper.poll_commands(radio, commands_path, state_path, "sensor1") == 0
    assert len(radio.sent) == 2


def test_receive_appends_record_with_mesh_time(tmp_path):
    path = tmp_path / "readings.jsonl"
    packet = {
        "from": 123,
        "id": 7,
        "rxTime": 1690000000,
        "decoded": {"text": "sensor1: 3;1.5"},
    }

    scrapper.on_receive(packet, None, readings_path=path, seen=scrapper.SeenPackets())

    assert json.loads(path.read_text()) == {
        "timestamp": "2023-07-22T04:26:40Z",
        "sensor": "sensor1",
        "tilts": 3,
        "rain_mm": 1.5,
    }


def test_receive_ignores_duplicate_packet(tmp_path):
    path = tmp_path / "readings.jsonl"
    seen = scrapper.SeenPackets()
    packet = {"from": 123, "id": 7, "rxTime": 1690000000, "decoded": {"text": "sensor1: 3;1.5"}}

    scrapper.on_receive(packet, None, readings_path=path, seen=seen)
    scrapper.on_receive(packet, None, readings_path=path, seen=seen)

    assert len(path.read_text().splitlines()) == 1


def test_receive_ignores_unrelated_text(tmp_path):
    path = tmp_path / "readings.jsonl"
    packet = {"from": 123, "id": 8, "rxTime": 1690000000, "decoded": {"text": "hello mesh"}}

    scrapper.on_receive(packet, None, readings_path=path, seen=scrapper.SeenPackets())

    assert not path.exists()


def test_receive_ignores_packet_without_text(tmp_path):
    path = tmp_path / "readings.jsonl"
    packet = {"from": 123, "id": 9, "rxTime": 1690000000}

    scrapper.on_receive(packet, None, readings_path=path, seen=scrapper.SeenPackets())

    assert not path.exists()


class FakeStop:
    def __init__(self, after=1):
        self.after = after
        self.calls = 0

    def is_set(self):
        return self.calls >= self.after

    def wait(self, timeout):
        self.calls += 1


def test_serve_sends_commands_while_radio_is_connected(tmp_path):
    commands_path = tmp_path / "commands.json"
    state_path = tmp_path / "state.json"
    commands_path.write_text(json.dumps([{"id": 1, "command": "reset"}]))
    radio = FakeRadio()

    scrapper.serve(
        radio,
        commands_path=commands_path,
        state_path=state_path,
        sensor="sensor1",
        channel=0,
        poll_interval=0,
        stop_event=FakeStop(),
    )

    assert radio.sent == [("sensor1;reset\n", 0)]


def test_serve_stops_when_radio_disconnects(tmp_path):
    class FakeState:
        def __init__(self, connected):
            self.connected = connected

        def is_set(self):
            return self.connected

    commands_path = tmp_path / "commands.json"
    state_path = tmp_path / "state.json"
    commands_path.write_text(json.dumps([{"id": 1, "command": "reset"}]))
    radio = FakeRadio()
    radio.isConnected = FakeState(False)

    scrapper.serve(
        radio,
        commands_path=commands_path,
        state_path=state_path,
        sensor="sensor1",
        channel=0,
        poll_interval=0,
        stop_event=FakeStop(after=1),
    )

    assert radio.sent == []


def test_pubsub_handler_delivers_packet(tmp_path):
    from pubsub import pub

    path = tmp_path / "readings.jsonl"
    handler = scrapper.make_handler(path, scrapper.SeenPackets())
    pub.subscribe(handler, "test.meshtastic.receive.text")
    try:
        pub.sendMessage(
            "test.meshtastic.receive.text",
            packet={
                "from": 1,
                "id": 2,
                "rxTime": 1690000000,
                "decoded": {"text": "sensor1: 1;0.2"},
            },
            interface=None,
        )
    finally:
        pub.unsubscribe(handler, "test.meshtastic.receive.text")

    assert json.loads(path.read_text())["rain_mm"] == 0.2


def test_handler_survives_storage_error(tmp_path, monkeypatch):
    def broken_append(path, record):
        raise OSError("brak miejsca")

    monkeypatch.setattr(scrapper, "append_record", broken_append)
    handler = scrapper.make_handler(tmp_path / "readings.jsonl", scrapper.SeenPackets())

    handler(
        {"from": 1, "id": 2, "rxTime": 1690000000, "decoded": {"text": "sensor1: 1;0.2"}},
        None,
    )
