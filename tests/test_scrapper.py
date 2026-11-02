import json

import scrapper


def test_parses_reading():
    assert scrapper.parse_reading("sensor1: 1234;20") == {
        "sensor": "sensor1",
        "tilts": 1234,
        "rain_mm": 20.0,
    }


def test_parses_reading_without_tilts():
    assert scrapper.parse_reading("sensor1;9") == {
        "sensor": "sensor1",
        "tilts": None,
        "rain_mm": 9.0,
    }


def test_parses_reading_without_tilts_tolerates_whitespace():
    assert scrapper.parse_reading("  sensor1 ; 20.5 \n") == {
        "sensor": "sensor1",
        "tilts": None,
        "rain_mm": 20.5,
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
        "sensor1;c",
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


def test_builds_record_without_tilts():
    assert scrapper.build_record("sensor1;20", rx_time=1690000000) == {
        "timestamp": "2023-07-22T04:26:40Z",
        "sensor": "sensor1",
        "tilts": None,
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


READING = {
    "timestamp": "2023-07-22T04:26:40Z",
    "sensor": "sensor1",
    "tilts": 3,
    "rain_mm": 1.5,
}


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.payload


class FakeOpener:
    """Stand-in for urllib.request.urlopen; records calls and returns payloads."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        payload = self.payloads.pop(0) if self.payloads else {}
        return FakeResponse(payload)

    @property
    def urls(self):
        return [call.full_url if hasattr(call, "full_url") else call for call in self.calls]


class FakeRadio:
    isConnected = True

    def __init__(self):
        self.sent = []

    def sendText(self, text, channelIndex=0):
        self.sent.append((text, channelIndex))


def test_upload_reading_posts_millimeters_and_device_timestamp():
    opener = FakeOpener({})

    assert scrapper.upload_reading("http://api:8080", READING, opener=opener) is True

    request = opener.calls[0]
    assert request.full_url == "http://api:8080/api/Device/upload"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "millimeters": 1.5,
        "deviceTimestamp": "2023-07-22T04:26:40Z",
    }


def test_upload_reading_tolerates_trailing_slash():
    opener = FakeOpener({})

    scrapper.upload_reading("http://api:8080/", READING, opener=opener)

    assert opener.urls == ["http://api:8080/api/Device/upload"]


def test_upload_reading_returns_false_on_api_error():
    def broken(request, timeout=None):
        raise OSError("connection refused")

    assert scrapper.upload_reading("http://api:8080", READING, opener=broken) is False


def test_poll_commands_sends_clean_command_when_pending():
    opener = FakeOpener({"isPending": True}, {"cleanNow": True})
    radio = FakeRadio()

    assert scrapper.poll_commands(radio, "http://api:8080", "sensor1", opener=opener) == 1

    assert radio.sent == [("sensor1;c\n", 0)]
    assert opener.urls == [
        "http://api:8080/api/Device/clean-status",
        "http://api:8080/api/Device/commands",
    ]


def test_poll_commands_does_nothing_when_no_clean_is_pending():
    opener = FakeOpener({"isPending": False})
    radio = FakeRadio()

    assert scrapper.poll_commands(radio, "http://api:8080", "sensor1", opener=opener) == 0

    assert radio.sent == []
    assert len(opener.calls) == 1


def test_poll_commands_keeps_request_pending_when_mesh_send_fails():
    class BrokenRadio:
        def sendText(self, text, channelIndex=0):
            raise OSError("radio offline")

    opener = FakeOpener({"isPending": True})

    assert scrapper.poll_commands(BrokenRadio(), "http://api:8080", "sensor1", opener=opener) == 0

    assert len(opener.calls) == 1


def test_poll_commands_tolerates_api_error():
    def broken(url, timeout=None):
        raise OSError("connection refused")

    radio = FakeRadio()

    assert scrapper.poll_commands(radio, "http://api:8080", "sensor1", opener=broken) == 0

    assert radio.sent == []


def test_receive_appends_reading_with_mesh_time(tmp_path):
    readings = tmp_path / "readings.jsonl"
    messages = tmp_path / "messages.jsonl"
    packet = {
        "from": 123,
        "id": 7,
        "rxTime": 1690000000,
        "channel": 0,
        "fromId": "!0000007b",
        "decoded": {"text": "sensor1: 3;1.5"},
    }

    scrapper.on_receive(
        packet,
        None,
        readings_path=readings,
        messages_path=messages,
        seen=scrapper.SeenPackets(),
    )

    assert json.loads(readings.read_text()) == {
        "timestamp": "2023-07-22T04:26:40Z",
        "sensor": "sensor1",
        "tilts": 3,
        "rain_mm": 1.5,
    }
    assert json.loads(messages.read_text()) == {
        "timestamp": "2023-07-22T04:26:40Z",
        "from": "!0000007b",
        "channel": 0,
        "packet_id": 7,
        "text": "sensor1: 3;1.5",
    }


def test_receive_uploads_reading_to_api(tmp_path, monkeypatch):
    uploaded = []
    monkeypatch.setattr(
        scrapper,
        "upload_reading",
        lambda api_url, record: uploaded.append((api_url, record)),
    )
    packet = {"from": 1, "id": 2, "rxTime": 1690000000, "decoded": {"text": "sensor1: 3;1.5"}}

    scrapper.on_receive(
        packet,
        None,
        readings_path=tmp_path / "readings.jsonl",
        messages_path=tmp_path / "messages.jsonl",
        seen=scrapper.SeenPackets(),
        api_url="http://api:8080",
    )

    assert uploaded == [("http://api:8080", READING)]


def test_receive_does_not_upload_without_api_url(tmp_path, monkeypatch):
    uploaded = []
    monkeypatch.setattr(
        scrapper,
        "upload_reading",
        lambda api_url, record: uploaded.append((api_url, record)),
    )
    packet = {"from": 1, "id": 2, "rxTime": 1690000000, "decoded": {"text": "sensor1: 3;1.5"}}

    scrapper.on_receive(
        packet,
        None,
        readings_path=tmp_path / "readings.jsonl",
        messages_path=tmp_path / "messages.jsonl",
        seen=scrapper.SeenPackets(),
    )

    assert uploaded == []


def test_receive_uploads_reading_without_tilts(tmp_path, monkeypatch):
    uploaded = []
    monkeypatch.setattr(
        scrapper,
        "upload_reading",
        lambda api_url, record: uploaded.append((api_url, record)),
    )
    packet = {"from": 9, "id": 8, "rxTime": 1690000000, "decoded": {"text": "sensor1;9"}}

    scrapper.on_receive(
        packet,
        None,
        readings_path=tmp_path / "readings.jsonl",
        messages_path=tmp_path / "messages.jsonl",
        seen=scrapper.SeenPackets(),
        api_url="http://api:8080",
    )

    assert uploaded == [
        (
            "http://api:8080",
            {
                "timestamp": "2023-07-22T04:26:40Z",
                "sensor": "sensor1",
                "tilts": None,
                "rain_mm": 9.0,
            },
        )
    ]


def test_receive_logs_every_message(tmp_path, caplog):
    packet = {"from": 9, "id": 8, "rxTime": 1690000000, "decoded": {"text": "sensor1;10"}}

    with caplog.at_level("INFO", logger="scrapper"):
        scrapper.on_receive(
            packet,
            None,
            readings_path=tmp_path / "readings.jsonl",
            messages_path=tmp_path / "messages.jsonl",
            seen=scrapper.SeenPackets(),
        )

    assert "sensor1;10" in caplog.text


def test_receive_does_not_upload_messages_that_are_not_readings(tmp_path, monkeypatch):
    uploaded = []
    monkeypatch.setattr(
        scrapper,
        "upload_reading",
        lambda api_url, record: uploaded.append((api_url, record)),
    )
    packet = {"from": 9, "id": 8, "rxTime": 1690000000, "decoded": {"text": "sensor1;c"}}

    scrapper.on_receive(
        packet,
        None,
        readings_path=tmp_path / "readings.jsonl",
        messages_path=tmp_path / "messages.jsonl",
        seen=scrapper.SeenPackets(),
        api_url="http://api:8080",
    )

    assert uploaded == []


def test_receive_saves_message_that_is_not_a_reading(tmp_path):
    readings = tmp_path / "readings.jsonl"
    messages = tmp_path / "messages.jsonl"
    packet = {"from": 9, "id": 8, "rxTime": 1690000000, "decoded": {"text": "sensor1;c"}}

    scrapper.on_receive(
        packet,
        None,
        readings_path=readings,
        messages_path=messages,
        seen=scrapper.SeenPackets(),
    )

    saved = json.loads(messages.read_text())
    assert saved["text"] == "sensor1;c"
    assert saved["from"] == 9
    assert saved["channel"] == 0
    assert not readings.exists()


def test_receive_ignores_duplicate_packet(tmp_path):
    readings = tmp_path / "readings.jsonl"
    messages = tmp_path / "messages.jsonl"
    seen = scrapper.SeenPackets()
    packet = {"from": 123, "id": 7, "rxTime": 1690000000, "decoded": {"text": "sensor1: 3;1.5"}}

    scrapper.on_receive(packet, None, readings_path=readings, messages_path=messages, seen=seen)
    scrapper.on_receive(packet, None, readings_path=readings, messages_path=messages, seen=seen)

    assert len(readings.read_text().splitlines()) == 1
    assert len(messages.read_text().splitlines()) == 1


def test_receive_ignores_packet_without_text(tmp_path):
    readings = tmp_path / "readings.jsonl"
    messages = tmp_path / "messages.jsonl"
    packet = {"from": 123, "id": 9, "rxTime": 1690000000}

    scrapper.on_receive(
        packet,
        None,
        readings_path=readings,
        messages_path=messages,
        seen=scrapper.SeenPackets(),
    )

    assert not readings.exists()
    assert not messages.exists()


class FakeStop:
    def __init__(self, after=1):
        self.after = after
        self.calls = 0

    def is_set(self):
        return self.calls >= self.after

    def wait(self, timeout):
        self.calls += 1


def test_serve_polls_api_while_radio_is_connected(monkeypatch):
    calls = []
    monkeypatch.setattr(scrapper, "poll_commands", lambda *args: calls.append(args))
    radio = FakeRadio()

    scrapper.serve(
        radio,
        api_url="http://api:8080",
        sensor="sensor1",
        channel=0,
        poll_interval=0,
        stop_event=FakeStop(),
    )

    assert calls == [(radio, "http://api:8080", "sensor1", 0)]


def test_serve_stops_when_radio_disconnects(monkeypatch):
    class FakeState:
        def __init__(self, connected):
            self.connected = connected

        def is_set(self):
            return self.connected

    calls = []
    monkeypatch.setattr(scrapper, "poll_commands", lambda *args: calls.append(args))
    radio = FakeRadio()
    radio.isConnected = FakeState(False)

    scrapper.serve(
        radio,
        api_url="http://api:8080",
        sensor="sensor1",
        channel=0,
        poll_interval=0,
        stop_event=FakeStop(after=1),
    )

    assert calls == []


def test_pubsub_handler_delivers_packet(tmp_path):
    from pubsub import pub

    path = tmp_path / "readings.jsonl"
    messages = tmp_path / "messages.jsonl"
    handler = scrapper.make_handler(path, messages, scrapper.SeenPackets())
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
    handler = scrapper.make_handler(
        tmp_path / "readings.jsonl",
        tmp_path / "messages.jsonl",
        scrapper.SeenPackets(),
    )

    handler(
        {"from": 1, "id": 2, "rxTime": 1690000000, "decoded": {"text": "sensor1: 1;0.2"}},
        None,
    )
