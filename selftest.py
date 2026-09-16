"""Testy automatyczne PSU19 Tester.

Uruchomienie:
    py -m unittest selftest -v
albo:
    run_tests.bat
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from config import AppConfig, ConfigError
from engine import TestEngine
from hardware import PM125Client, SimulatedPM125
from models import (
    FinalStatus,
    Measurement,
    ProfileResult,
    ProfileStatus,
    TestProfile,
    TestResult,
)
from storage import AttemptStore, CsvResultRepository, PendingRecord, PendingResultStore, RawSampleWriter


# --------------------------------------------------------------- atrapy


class FakeHardware:
    """Konfigurowalna atrapa PM125 do testow silnika."""

    def __init__(
        self,
        voltage: float = 5.0,
        current_follows_load: bool = True,
        read_error: bool = False,
        ramp_seconds: float = 0.0,
        fail_profile_index: int | None = None,
    ) -> None:
        self.connected = True
        self.last_error = ""
        self.voltage = voltage
        self.current_follows_load = current_follows_load
        self.read_error = read_error
        self.ramp_seconds = ramp_seconds
        self.fail_profile_index = fail_profile_index
        self.load_ma = 0
        self.profile_index = 1
        self.load_changed_at = time.monotonic()
        self.reset_calls = 0
        self.zero_load_calls = 0

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        self.connected = False

    def ping(self) -> bool:
        return self.connected

    def set_profile(self, index: int) -> bool:
        self.profile_index = index
        self.load_changed_at = time.monotonic()
        return True

    def set_load(self, current_ma: int) -> bool:
        if current_ma == 0:
            self.zero_load_calls += 1
        self.load_ma = current_ma
        self.load_changed_at = time.monotonic()
        return True

    def read_measurement(self) -> Measurement | None:
        if self.read_error or (
            self.fail_profile_index is not None and self.profile_index == self.fail_profile_index
        ):
            self.last_error = "symulowany błąd odczytu"
            return None
        target = self.load_ma / 1000.0 if self.current_follows_load else 0.0
        if self.ramp_seconds > 0:
            elapsed = time.monotonic() - self.load_changed_at
            target *= min(1.0, elapsed / self.ramp_seconds)
        return Measurement(0.0, self.voltage, target)

    def safe_reset(self) -> bool:
        self.reset_calls += 1
        self.load_ma = 0
        return True


def fast_config(**overrides) -> AppConfig:
    config = AppConfig(**overrides)
    config.measurement_interval = 0.01
    config.profile_settle_seconds = 0.0
    config.load_settle_seconds = 0.0
    config.settling_ignore_seconds = 0.0
    config.profiles = [
        TestProfile("5V", 1, 5.0, 4.75, 5.50, 0.25, 0.25, 3000),
        TestProfile("9V", 2, 9.0, 8.55, 9.45, 0.25, 0.25, 3000),
    ]
    config.minimum_samples = 2
    return config


# ------------------------------------------------------------- modele


class ProfileResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = TestProfile("5V", 1, 5.0, 4.75, 5.50, 2.5, 2.5, 3000)

    def test_pass_requires_both_phases(self) -> None:
        result = ProfileResult(profile=self.profile)
        for _ in range(3):
            result.add(Measurement(0.0, 5.0, 0.0), loaded=False)
        result.finalize(3)
        self.assertIs(result.status, ProfileStatus.NO_DATA)

    def test_valid_measurements_pass(self) -> None:
        result = ProfileResult(profile=self.profile)
        for _ in range(3):
            result.add(Measurement(0.0, 5.0, 0.01), loaded=False)
            result.add(Measurement(0.0, 5.0, 3.0), loaded=True)
        result.finalize(3)
        self.assertIs(result.status, ProfileStatus.PASS)

    def test_current_is_part_of_acceptance(self) -> None:
        result = ProfileResult(profile=self.profile)
        for _ in range(3):
            result.add(Measurement(0.0, 5.0, 0.0), loaded=False)
            result.add(Measurement(0.0, 5.0, 0.0), loaded=True)
        result.finalize(3)
        self.assertIs(result.status, ProfileStatus.FAIL)
        self.assertIn("prąd", result.violation)

    def test_violation_names_phase_and_value(self) -> None:
        result = ProfileResult(profile=self.profile)
        for _ in range(3):
            result.add(Measurement(0.0, 4.0, 0.0), loaded=False)
            result.add(Measurement(0.0, 5.0, 3.0), loaded=True)
        result.finalize(3)
        self.assertIs(result.status, ProfileStatus.FAIL)
        self.assertIn("bez obciążenia", result.violation)
        self.assertIn("napięcie", result.violation)

    def test_settling_samples_are_not_rated(self) -> None:
        result = ProfileResult(profile=self.profile)
        # Próbki z okna stabilizacji sa poza zakresem, ale nie mogą decydowac o wyniku.
        for _ in range(3):
            result.add(Measurement(0.0, 1.0, 0.0, settling=True), loaded=False)
            result.add(Measurement(0.0, 1.0, 0.0, settling=True), loaded=True)
        for _ in range(3):
            result.add(Measurement(1.0, 5.0, 0.01), loaded=False)
            result.add(Measurement(1.0, 5.0, 3.0), loaded=True)
        result.finalize(3)
        self.assertIs(result.status, ProfileStatus.PASS)
        self.assertEqual(result.sample_count, 12)


# -------------------------------------------------------------- silnik


class EngineTests(unittest.TestCase):
    def test_pass(self) -> None:
        config = fast_config()
        hardware = FakeHardware()
        hardware.voltage = 5.0
        config.profiles = [config.profiles[0]]
        result = TestEngine(config, hardware).run("TEST", "SN00001")
        self.assertIs(result.final_status, FinalStatus.PASS)

    def test_voltage_fail(self) -> None:
        config = fast_config()
        config.profiles = [config.profiles[0]]
        hardware = FakeHardware(voltage=4.0)
        result = TestEngine(config, hardware).run("TEST", "SN00001")
        self.assertIs(result.final_status, FinalStatus.FAIL)
        self.assertIn("napięcie", result.first_violation)

    def test_read_error_is_not_product_fail(self) -> None:
        config = fast_config()
        hardware = FakeHardware(read_error=True)
        result = TestEngine(config, hardware).run("TEST", "SN00001")
        self.assertIs(result.final_status, FinalStatus.ERROR)

    def test_station_error_outranks_product_fail(self) -> None:
        """Rozdz. 2.1: FAIL na jednym profilu + błąd stanowiska na kolejnym = ERROR."""
        config = fast_config()
        hardware = FakeHardware(voltage=4.0, fail_profile_index=2)
        result = TestEngine(config, hardware).run("TEST", "SN00001")
        self.assertIs(result.final_status, FinalStatus.ERROR)
        self.assertIs(result.profiles[0].status, ProfileStatus.FAIL)
        self.assertIs(result.profiles[1].status, ProfileStatus.MEASUREMENT_ERROR)

    def test_settling_window_prevents_false_fail(self) -> None:
        """Rozdz. 3.1: rampa prądu po skoku obciążenia nie może odrzucać dobrych sztuk."""
        profile = TestProfile("5V", 1, 5.0, 4.75, 5.50, 1.2, 1.2, 3000)

        config_without = fast_config()
        config_without.profiles = [profile]
        config_without.settling_ignore_seconds = 0.0
        result_without = TestEngine(config_without, FakeHardware(ramp_seconds=0.4)).run("TEST", "SN1")
        self.assertIs(result_without.final_status, FinalStatus.FAIL)

        config_with = fast_config()
        config_with.profiles = [profile]
        config_with.settling_ignore_seconds = 0.6
        result_with = TestEngine(config_with, FakeHardware(ramp_seconds=0.4)).run("TEST", "SN1")
        self.assertIs(result_with.final_status, FinalStatus.PASS)

    def test_load_is_zeroed_even_after_error(self) -> None:
        """Rozdz. 2.3: obciążenie zerowane po każdym profilu, także po błędzie."""
        config = fast_config()
        hardware = FakeHardware(read_error=True)
        TestEngine(config, hardware).run("TEST", "SN00001")
        self.assertGreaterEqual(hardware.zero_load_calls, 1)
        self.assertGreaterEqual(hardware.reset_calls, 1)

    def test_timeout_inside_profile(self) -> None:
        config = fast_config()
        config.test_timeout = 0.05
        config.profiles[0] = TestProfile("5V", 1, 5.0, 4.75, 5.50, 5.0, 5.0, 3000)
        result = TestEngine(config, FakeHardware()).run("TEST", "SN00001")
        self.assertIs(result.final_status, FinalStatus.TIMEOUT)

    def test_cancel(self) -> None:
        config = fast_config()
        config.profiles[0] = TestProfile("5V", 1, 5.0, 4.75, 5.50, 5.0, 5.0, 3000)
        cancel = threading.Event()
        cancel.set()
        result = TestEngine(config, FakeHardware()).run("TEST", "SN00001", cancel_event=cancel)
        self.assertIs(result.final_status, FinalStatus.CANCELLED)

    def test_dut_precheck_detects_missing_psu(self) -> None:
        """Rozdz. 6.6: pusty stelaż kończy się komunikatem, a nie pełnym testem."""
        config = fast_config()
        hardware = FakeHardware(voltage=0.0)
        detected, voltage, message = TestEngine(config, hardware).detect_dut()
        self.assertFalse(detected)
        self.assertIn("Nie wykryto zasilacza", message)
        self.assertEqual(voltage, 0.0)

    def test_dut_precheck_accepts_present_psu(self) -> None:
        config = fast_config()
        detected, _voltage, _message = TestEngine(config, FakeHardware(voltage=5.0)).detect_dut()
        self.assertTrue(detected)


# -------------------------------------------------------------- sprzęt


class HardwareTests(unittest.TestCase):
    def test_constructor_does_not_require_device_or_executable(self) -> None:
        client = PM125Client("C:/nieistnieje/USBPDConsole.exe", None, 1.0)
        self.assertFalse(client.connected)

    def test_connect_and_measurement_parsing(self) -> None:
        client = PM125Client("dowolny.exe", None, 1.0)
        with patch.object(PM125Client, "_run", return_value="STATUS:CONNECTED"), \
                patch("pathlib.Path.is_file", return_value=True):
            self.assertTrue(client.connect())
        with patch.object(
            PM125Client, "_run", return_value="VOLTAGE: 5012 mV\nMEASURED CURRENT: 2980 mA"
        ):
            measurement = client.read_measurement()
        self.assertIsNotNone(measurement)
        self.assertAlmostEqual(measurement.voltage, 5.012, places=3)
        self.assertAlmostEqual(measurement.current, 2.980, places=3)

    def test_measurement_parsing_accepts_volts_and_amps(self) -> None:
        client = PM125Client("dowolny.exe", None, 1.0)
        with patch.object(
            PM125Client, "_run", return_value="VOLTAGE: 5,012 V  MEASURED CURRENT: 2,98 A"
        ):
            measurement = client.read_measurement()
        self.assertAlmostEqual(measurement.voltage, 5.012, places=3)
        self.assertAlmostEqual(measurement.current, 2.98, places=3)

    def test_command_error_does_not_drop_connection(self) -> None:
        """Rozdz. 2.3: błąd pojedynczej komendy nie jest utrata urządzenia."""
        client = PM125Client("dowolny.exe", None, 1.0)
        client.connected = True
        client._set_error("błąd pojedynczej komendy")
        self.assertTrue(client.connected)
        client._set_error("brak urządzenia", fatal=True)
        self.assertFalse(client.connected)

    def test_rejected_command_is_detected(self) -> None:
        client = PM125Client("dowolny.exe", None, 1.0)
        with patch.object(PM125Client, "_run", return_value="ERROR: invalid profile"):
            self.assertFalse(client.set_profile(9))
        self.assertIn("odrzucił", client.last_error)

    def test_simulator_ramps_current(self) -> None:
        simulator = SimulatedPM125("pass", ramp_seconds=0.5)
        simulator.connect()
        simulator.set_load(3000)
        first = simulator.read_measurement()
        self.assertLess(first.current, 3.0)


# -------------------------------------------------------------- magazyn


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _result(self, profiles: list[TestProfile], status: FinalStatus = FinalStatus.PASS) -> TestResult:
        items = []
        for profile in profiles:
            item = ProfileResult(profile=profile, status=ProfileStatus.PASS)
            item.add(Measurement(0.0, profile.nominal_voltage, 0.01), loaded=False)
            item.add(Measurement(0.0, profile.nominal_voltage, profile.load_current_ma / 1000), loaded=True)
            items.append(item)
        return TestResult("TEST", "SN00001", items, status, 10.0)

    def test_primary_save(self) -> None:
        repo = CsvResultRepository(self.root / "reports", self.root / "backup", 1000)
        outcome = repo.save(self._result(AppConfig().profiles))
        self.assertTrue(outcome.saved)
        self.assertTrue(outcome.path.exists())

    def test_backup_save(self) -> None:
        (self.root / "reports").write_text("blokada", encoding="utf-8")  # ścieżka nie jest katalogiem
        repo = CsvResultRepository(self.root / "reports", self.root / "backup", 1000)
        outcome = repo.save(self._result(AppConfig().profiles), retries=1, retry_delay=0)
        self.assertTrue(outcome.saved)
        self.assertIn("backup", str(outcome.path))

    def test_profile_change_starts_new_report(self) -> None:
        """Rozdz. 2.2: zmiana zestawu profili nie może rozjechać kolumn raportu."""
        repo = CsvResultRepository(self.root / "reports", self.root / "backup", 1000)
        all_profiles = AppConfig().profiles
        first = repo.save(self._result(all_profiles))
        second = repo.save(self._result(all_profiles[:2]))
        self.assertNotEqual(first.path, second.path)
        for path, expected in ((first.path, all_profiles), (second.path, all_profiles[:2])):
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(len(lines[0].split(";")), len(lines[1].split(";")))
            self.assertEqual(len(lines[0].split(";")), len(TestResult.csv_headers(
                [item.name for item in expected]
            )))

    def test_row_limit_rolls_file(self) -> None:
        repo = CsvResultRepository(self.root / "reports", self.root / "backup", 3)
        profiles = AppConfig().profiles
        paths = {repo.save(self._result(profiles)).path for _ in range(4)}
        self.assertGreaterEqual(len(paths), 2)

    def test_attempts_are_persistent_and_migrate_old_format(self) -> None:
        path = self.root / "serial_attempts.json"
        path.write_text(json.dumps({"SN00001": 1}), encoding="utf-8")
        store = AttemptStore(path)
        self.assertEqual(store.count("sn00001"), 1)
        self.assertEqual(store.record("SN00001"), 2)
        reloaded = AttemptStore(path)
        self.assertEqual(reloaded.count("SN00001"), 2)
        reloaded.reset("SN00001")
        self.assertEqual(AttemptStore(path).count("SN00001"), 0)

    def test_attempts_entries_expose_last_use(self) -> None:
        store = AttemptStore(self.root / "attempts.json")
        store.record("SN00002")
        entries = store.entries()
        self.assertEqual(entries[0][0], "SN00002")
        self.assertEqual(entries[0][1], 1)
        self.assertTrue(entries[0][2])

    def test_pending_results_survive_restart(self) -> None:
        """Rozdz. 4.2: niezapisany wynik nie ginie razem z procesem."""
        path = self.root / "pending.json"
        store = PendingResultStore(path)
        record = PendingRecord.from_result(self._result(AppConfig().profiles, FinalStatus.FAIL))
        store.add(record)
        self.assertEqual(PendingResultStore(path).count, 1)
        restored = PendingResultStore(path).all()[0]
        self.assertEqual(restored.serial_number, "SN00001")
        store.remove(record)
        self.assertEqual(PendingResultStore(path).count, 0)

    def test_raw_samples_are_written(self) -> None:
        """Rozdz. 3.3: surowe próbki trafiają do osobnego pliku."""
        writer = RawSampleWriter(self.root / "raw", enabled=True)
        path = writer.write(self._result(AppConfig().profiles))
        self.assertIsNotNone(path)
        content = path.read_text(encoding="utf-8-sig")
        self.assertIn("Profil;Faza", content)
        self.assertIn("5V", content)


# --------------------------------------------------------- konfiguracja


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "test_config.json"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_round_trip(self) -> None:
        config = AppConfig()
        config.save(self.path)
        loaded, warnings = AppConfig.load(self.path)
        self.assertEqual(loaded.profiles[0].name, config.profiles[0].name)
        self.assertEqual(warnings, [])

    def test_defaults_match_product_specification(self) -> None:
        """Rozdz. 3.2: zakresy zgodne ze specyfikacja SR213 PSU."""
        expected = {
            "5V": (4.75, 5.50, 3000),
            "9V": (8.55, 9.45, 3000),
            "12V": (11.40, 12.60, 3000),
            "15V": (14.25, 15.75, 2400),
        }
        for profile in AppConfig().profiles:
            minimum, maximum, current = expected[profile.name]
            self.assertAlmostEqual(profile.min_voltage, minimum, places=2)
            self.assertAlmostEqual(profile.max_voltage, maximum, places=2)
            self.assertEqual(profile.load_current_ma, current)

    def test_invalid_config_is_rejected(self) -> None:
        config = AppConfig()
        config.minimum_samples = 0
        with self.assertRaises(ConfigError):
            config.save(self.path)

    def test_timeout_shorter_than_test_is_rejected(self) -> None:
        config = AppConfig()
        config.test_timeout = 5.0
        self.assertTrue(any("Timeout" in item for item in config.validate()))

    def test_settling_window_must_fit_in_phase(self) -> None:
        config = AppConfig()
        config.settling_ignore_seconds = 5.0
        self.assertTrue(any("okno stabilizacji" in item for item in config.validate()))

    def test_unknown_keys_do_not_block_startup(self) -> None:
        """Rozdz. 4.6: nieznany klucz ma być ostrzeżeniem, nie zatrzymaniem stanowiska."""
        payload = json.loads(json.dumps(AppConfig().to_payload()))
        payload["nieznany_klucz"] = 1
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        config, warnings = AppConfig.load(self.path)
        self.assertTrue(config.profiles)
        self.assertTrue(any("Nieznany klucz" in item for item in warnings))

    def test_corrupted_file_is_restored_from_backup(self) -> None:
        AppConfig().save(self.path)
        self.path.with_suffix(self.path.suffix + ".bak").write_text(
            self.path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.path.write_text("{ to nie jest json", encoding="utf-8")
        config, warnings = AppConfig.load(self.path)
        self.assertTrue(config.profiles)
        self.assertTrue(any("uszkodzony" in item for item in warnings))

    def test_audit_entry_is_written(self) -> None:
        """Rozdz. 4.9: zmiana konfiguracji zostawia ślad."""
        config = AppConfig()
        config.audit_file = str(self.path.parent / "config_audit.log")
        config.logs_directory = str(self.path.parent / "logs")
        config.save(self.path, audit_note="test audytu")
        self.assertIn("test audytu", Path(config.audit_file).read_text(encoding="utf-8"))

    def test_estimated_time_matches_requirement(self) -> None:
        """Wymaganie procesowe: 2,5 s + 2,5 s na każdy z 4 profili = 20 s pomiarów."""
        config = AppConfig()
        measurement_only = sum(
            profile.no_load_seconds + profile.load_seconds for profile in config.profiles
        )
        self.assertAlmostEqual(measurement_only, 20.0, places=3)
        self.assertLess(config.estimated_test_seconds(), config.test_timeout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
