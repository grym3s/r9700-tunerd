"""Tests for the `doctor` / `doctor --json` diagnostics subcommand and the
redacted support bundle (`doctor --bundle` / `--bundle-preview`).

The asleep-case test is the acceptance criterion for this card: it records
every path rt.read_text() is asked to read while the fake card is
runtime-suspended, runs both `doctor` and `doctor --json`, and asserts the
recorded set contains none of the unsafe sysfs leaves (hwmon, pp_*,
mem_info_*, gpu_busy_percent).
"""
from __future__ import annotations

import json
import tarfile

import pytest

from conftest import rt


UNSAFE_NAMES = (
    "pp_od_clk_voltage",
    "pp_power_profile_mode",
    "mem_info_gtt_total",
    "mem_info_vram_used",
    "mem_info_vram_total",
    "gpu_busy_percent",
    "power1_cap",
    "power1_cap_min",
    "power1_cap_max",
    "power1_cap_default",
)


def _make_asleep(fake_tree):
    pci = fake_tree / "0000:aa:00.0"
    (pci / "power" / "runtime_status").write_text("suspended\n")
    (pci / "power_state").write_text("D3cold\n")
    return pci


@pytest.fixture(autouse=True)
def _isolate_host_paths(monkeypatch, tmp_path):
    """Never let doctor touch the real host's /etc/sudoers.d or
    /usr/local/sbin during tests; point both at tmp_path locations that
    don't exist unless a test creates them."""
    monkeypatch.setattr(rt, "SUDOERS_RULE_PATH", tmp_path / "sudoers-rule-absent")
    monkeypatch.setattr(rt, "INSTALLED_DAEMON_PATH", tmp_path / "installed-absent")


# ---------------------------------------------------------------------------
# The acceptance test: asleep case, zero unsafe reads.
# ---------------------------------------------------------------------------


def test_doctor_asleep_reads_no_unsafe_path(fake_tree, conf, monkeypatch, capsys):
    _make_asleep(fake_tree)

    recorded_reads: list[str] = []
    real_read_text = rt.read_text

    def recording_read_text(path):
        recorded_reads.append(str(path))
        return real_read_text(path)

    monkeypatch.setattr(rt, "read_text", recording_read_text)

    assert rt.cmd_doctor(conf, as_json=False) == 0
    assert rt.cmd_doctor(conf, as_json=True) == 0
    capsys.readouterr()

    for name in UNSAFE_NAMES:
        for r in recorded_reads:
            assert not r.endswith(name), f"unsafe read while asleep: {r}"


def test_doctor_asleep_never_opens_dri(fake_tree, conf, monkeypatch):
    """doctor never touches /dev/dri regardless of state."""
    _make_asleep(fake_tree)
    real_open = open

    def guarded_open(path, *a, **kw):
        assert "/dev/dri" not in str(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", guarded_open)
    assert rt.cmd_doctor(conf, as_json=True) == 0


def test_doctor_asleep_capabilities_are_existence_only(fake_tree, conf):
    """Capability inventory reflects presence without reading contents."""
    _make_asleep(fake_tree)
    payload = rt.build_doctor_payload(conf)
    caps = payload["capabilities"]
    # The fake tree does create pp_od_clk_voltage and hwmon; doctor must
    # report they *exist* without having read their contents (verified by
    # the read-recording test above).
    assert caps["pp_od_clk_voltage"] is True
    assert caps["hwmon"] is True
    assert caps["gpu_busy_percent"] is False


# ---------------------------------------------------------------------------
# Active case: sanity, and schema/field-name parity with status --json.
# ---------------------------------------------------------------------------


def test_doctor_json_schema_and_evict_guard_field_names(fake_tree, conf):
    payload = rt.build_doctor_payload(conf)
    assert payload["schema_version"] == 1
    eg = payload["evict_guard"]
    assert set(eg.keys()) == {"enabled", "holding", "vram_used", "gtt_total", "margin"}


def test_doctor_identity_discovered_at_runtime_not_hardcoded(fake_tree, conf):
    payload = rt.build_doctor_payload(conf)
    ident = payload["identity"]
    assert ident["pci"] == "0000:aa:00.0"
    assert ident["matched"] == ident["want"]


def test_doctor_version_field_present(fake_tree, conf):
    payload = rt.build_doctor_payload(conf)
    assert payload["version"]["repo"] == rt.__version__


def test_doctor_sudoers_rule_reports_existence_not_contents(fake_tree, conf, monkeypatch, tmp_path):
    rule = tmp_path / "sudoers-rule"
    rule.write_text("secret sudoers content\n")
    monkeypatch.setattr(rt, "SUDOERS_RULE_PATH", rule)
    payload = rt.build_doctor_payload(conf)
    assert payload["sudoers_rule_present"] is True
    dumped = json.dumps(payload)
    assert "secret sudoers content" not in dumped


def test_doctor_human_output_active(fake_tree, conf, capsys):
    assert rt.cmd_doctor(conf, as_json=False) == 0
    out = capsys.readouterr().out
    assert "runtime_pm:" in out
    assert "identity:" in out
    assert "evict_guard:" in out


def test_doctor_json_output_is_valid_json(fake_tree, conf, capsys):
    assert rt.cmd_doctor(conf, as_json=True) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["schema_version"] == 1


# ---------------------------------------------------------------------------
# --bundle / --bundle-preview
# ---------------------------------------------------------------------------


def test_bundle_preview_lists_entries_without_writing(fake_tree, conf, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **kw: pytest.raises)
    # Avoid real journalctl/subprocess calls entirely.
    def fake_run(cmd, **kw):
        class R:
            stdout = ""
        return R()
    monkeypatch.setattr(rt.subprocess, "run", fake_run)

    conf_path = tmp_path / "r9700-tunerd.conf"
    conf_path.write_text("VOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_path)

    assert rt.cmd_doctor_bundle(conf, None, preview=True) == 0
    out = capsys.readouterr().out
    assert "doctor.json" in out
    assert not (tmp_path / "bundle.tar.gz").exists()


def test_bundle_writes_redacted_tarball(fake_tree, conf, monkeypatch, tmp_path, capsys):
    def fake_run(cmd, **kw):
        class R:
            stdout = "line with /home/grymes/secret and token deadbeefdeadbeefdeadbeefdeadbeef\n"
        return R()
    monkeypatch.setattr(rt.subprocess, "run", fake_run)

    conf_path = tmp_path / "r9700-tunerd.conf"
    conf_path.write_text("VOLTAGE_OFFSET_MV=0  # /home/grymes/notes\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_path)

    out_path = tmp_path / "bundle.tar.gz"
    assert rt.cmd_doctor_bundle(conf, out_path, preview=False) == 0
    assert out_path.exists()

    with tarfile.open(out_path, "r:gz") as tf:
        names = tf.getnames()
        assert "doctor.json" in names
        assert "journal.txt" in names
        journal_text = tf.extractfile("journal.txt").read().decode("utf-8")
        assert "/home/grymes" not in journal_text
        assert "~" in journal_text
        assert "deadbeefdeadbeefdeadbeefdeadbeef" not in journal_text

        conf_text = tf.extractfile("r9700-tunerd.conf").read().decode("utf-8")
        assert "/home/grymes" not in conf_text
        assert "~" in conf_text


def test_bundle_never_contains_dashboard_token_pattern(fake_tree, conf, monkeypatch, tmp_path):
    """A 32-hex-char token (secrets.token_hex(16) shape) must never survive redaction."""
    def fake_run(cmd, **kw):
        class R:
            stdout = ""
        if cmd and cmd[0] == "journalctl":
            R.stdout = "TOKEN: abcdef0123456789abcdef0123456789 issued\n"
        return R()
    monkeypatch.setattr(rt.subprocess, "run", fake_run)

    conf_path = tmp_path / "r9700-tunerd.conf"
    conf_path.write_text("VOLTAGE_OFFSET_MV=0\n")
    monkeypatch.setattr(rt, "CONF_PATH", conf_path)

    entries = rt._bundle_entries(conf)
    for name, data in entries:
        assert b"abcdef0123456789abcdef0123456789" not in data
