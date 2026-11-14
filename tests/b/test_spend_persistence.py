"""Model spend survives a restart, so the ceiling cannot be reset by restarting the server."""

from atlas.outbound import SpendMeter


def test_spend_is_kept_on_disk(tmp_path):
    path = tmp_path / "local" / "normal-spend.json"
    seen: list[str] = []
    meter = SpendMeter(10.0, notify=lambda event, amount: seen.append(event), path=path)
    meter.add(4.0)
    meter.add(4.5)  # crosses 80%
    assert seen == ["spend.80"]

    again = SpendMeter(10.0, notify=lambda event, amount: seen.append(event), path=path)
    assert again.spent == 8.5
    again.add(0.1)  # the 80% notice is not sent twice after a restart
    assert seen == ["spend.80"]
    again.add(2.0)
    assert seen == ["spend.80", "spend.100"]
    assert SpendMeter(10.0, path=path).hold_reason() == "spend ceiling reached"


def test_damaged_file_starts_at_zero(tmp_path):
    path = tmp_path / "spend.json"
    path.write_text("not json", encoding="utf-8")
    assert SpendMeter(5.0, path=path).spent == 0.0


def test_no_path_keeps_spend_in_memory():
    meter = SpendMeter(5.0)
    meter.add(1.0)
    assert meter.spent == 1.0
