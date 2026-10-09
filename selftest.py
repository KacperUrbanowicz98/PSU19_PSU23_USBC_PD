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

from config import AppConfig, ConfigError, StationConfig, station_names_for, stations_from_devices
from detection import plan_station_remap
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
from loop_test import LoopReportWriter, LoopRunner, build_report_lines
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
        follow_profile: bool = False,
        offset: float = 0.0,
    ) -> None:
        self.connected = True
        self.last_error = ""
        self.voltage = voltage
        #: Gdy True, atrapa zwraca napięcie nominalne ustawionego profilu (+ offset).
        self.follow_profile = follow_profile
        self.offset = offset
        self.current_follows_load = current_follows_load
        self.read_error = read_error
        self.ramp_seconds = ramp_seconds
        self.fail_profile_index = fail_profile_index
        self.load_ma = 0
        self.profile_index = 1
        self.load_changed_at = time.monotonic()
        self.reset_calls = 0
        self.zero_load_calls = 0
        self.command_calls = 0
        self.command_seconds = 0.0

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        self.connected = False

    def ping(self) -> bool:
        return self.connected

    def set_profile(self, index: int, force: bool = False) -> bool:
        self.profile_index = index
        self.load_changed_at = time.monotonic()
        self.command_calls += 1
        return True

    def set_load(self, current_ma: int, force: bool = False) -> bool:
        if current_ma == 0:
            self.zero_load_calls += 1
        self.load_ma = current_ma
        self.load_changed_at = time.monotonic()
        self.command_calls += 1
        return True

    def dut_profile_count(self) -> int | None:
        if self.read_error:
            return None
        return 0 if self.voltage <= 0.5 and not self.follow_profile else 4

    def read_configuration(self) -> str | None:
        return "CABLE RESISTANCE: 80 Ohms\nHOLD LOAD ON VOLTAGE CHANGES: DISABLED"

    @property
    def average_command_seconds(self) -> float:
        return self.command_seconds

    def reset_command_statistics(self) -> None:
        self.command_calls = 0

    def forget_state(self) -> None:
        pass

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
        voltage = self.voltage
        if self.follow_profile:
            voltage = {1: 5.0, 2: 9.0, 3: 12.0, 4: 15.0}.get(self.profile_index, 5.0) + self.offset
        return Measurement(0.0, voltage, target)

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


class SlowConsoleTests(unittest.TestCase):
    """Zachowanie przy konsoli PM125 odpowiadającej w kilkaset ms."""

    def test_settling_sample_count_protects_slow_console(self) -> None:
        """Przy wolnej konsoli pierwsza próbka fazy i tak nie może być oceniana."""
        profile = TestProfile("5V", 1, 5.0, 4.75, 5.50, 2.5, 2.5, 3000)
        config = AppConfig()
        config.profiles = [profile]
        config.measurement_interval = 0.01
        config.profile_settle_seconds = 0.0
        config.load_settle_seconds = 0.0
        config.settling_ignore_seconds = 0.0   # okno czasowe wyłączone
        config.settling_ignore_samples = 1     # zostaje zabezpieczenie indeksowe
        hardware = FakeHardware(ramp_seconds=0.9)
        hardware.command_seconds = 0.0
        result = TestEngine(config, hardware).run("TEST", "SN00001")
        first_loaded = result.profiles[0].loaded[0]
        self.assertTrue(first_loaded.settling)
        self.assertNotIn(first_loaded, result.profiles[0].rated_loaded)

    def test_phase_does_not_overrun_when_samples_are_complete(self) -> None:
        """Faza nie przeciąga się o czas komendy, gdy komplet próbek już jest."""
        profile = TestProfile("5V", 1, 5.0, 4.75, 5.50, 1.0, 1.0, 3000)
        config = AppConfig()
        config.profiles = [profile]
        config.measurement_interval = 0.0001
        config.profile_settle_seconds = 0.0
        config.load_settle_seconds = 0.0
        config.settling_ignore_seconds = 0.0
        config.settling_ignore_samples = 0
        config.minimum_samples = 2
        hardware = FakeHardware()
        hardware.command_seconds = 0.3  # deklarowany czas komendy
        result = TestEngine(config, hardware).run("TEST", "SN00001")
        # 2 fazy po 1 s: bez wcześniejszego kończenia test trwałby wyraźnie dłużej.
        self.assertLess(result.duration_seconds, 2.3)
        self.assertIs(result.final_status, FinalStatus.PASS)

    def test_config_rejects_setup_without_enough_rated_samples(self) -> None:
        """Konfiguracja, w której nie zmieszczą się oceniane próbki, jest błędem."""
        config = AppConfig()
        config.command_overhead_seconds = 2.0   # jedna próbka na fazę
        config.settling_ignore_samples = 1
        config.minimum_samples = 2
        errors = config.validate()
        self.assertTrue(any("ocenianych" in item for item in errors), errors)

    def test_estimate_includes_command_overhead(self) -> None:
        config = AppConfig()
        self.assertGreater(config.estimated_test_seconds(), config.measurement_seconds())
        self.assertEqual(config.measurement_seconds(), 20.0)


class LoopValidationTests(unittest.TestCase):
    """Tryb walidacyjny: seria, statystyka i raport."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.config = fast_config()
        self.config.validation_directory = str(self.root / "validation")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_series_counts_and_does_not_touch_attempts(self) -> None:
        engine = TestEngine(self.config, FakeHardware(follow_profile=True))
        summary, results = LoopRunner(self.config, engine).run(
            label="WAL", hrid="TEST", cycles=3, pause_seconds=0.0
        )
        self.assertEqual(summary.cycles_done, 3)
        self.assertEqual(summary.passed, 3)
        self.assertEqual(len(results), 3)
        self.assertTrue(summary.repeatable)
        # Seria nie korzysta z licznika prób ani z repozytorium produkcyjnego.
        self.assertFalse((self.root / "serial_attempts.json").exists())
        self.assertFalse((self.root / "reports").exists())

    def test_margin_and_statistics(self) -> None:
        """Zapas do granic liczony jest z najgorszego pomiaru serii."""
        # +0,4 V do nominału: 9 V wychodzi 9,40 przy granicy 9,45 — zapas 50 mV.
        engine = TestEngine(self.config, FakeHardware(follow_profile=True, offset=0.4))
        summary, _ = LoopRunner(self.config, engine).run(
            label="WAL", hrid="TEST", cycles=3, pause_seconds=0.0
        )
        worst = summary.worst_phase
        self.assertIsNotNone(worst)
        self.assertLess(worst.worst_margin, 0.1)
        self.assertGreater(worst.worst_margin, 0.0)
        for item in summary.phases:
            self.assertGreater(item.samples, 0)
            self.assertLessEqual(item.minimum, item.average)
            self.assertLessEqual(item.average, item.maximum)

    def test_error_cycles_mark_station_not_ready(self) -> None:
        engine = TestEngine(self.config, FakeHardware(read_error=True))
        summary, _ = LoopRunner(self.config, engine).run(
            label="WAL", hrid="TEST", cycles=2, pause_seconds=0.0
        )
        self.assertEqual(summary.errors, 2)
        report = "\n".join(build_report_lines(summary))
        self.assertIn("NIEGOTOWE", report)

    def test_cancelled_series_is_marked(self) -> None:
        cancel = threading.Event()
        cancel.set()
        engine = TestEngine(self.config, FakeHardware(follow_profile=True))
        summary, _ = LoopRunner(self.config, engine).run(
            label="WAL", hrid="TEST", cycles=5, pause_seconds=0.0, cancel_event=cancel
        )
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.cycles_done, 0)

    def test_report_files_are_written(self) -> None:
        engine = TestEngine(self.config, FakeHardware(follow_profile=True))
        summary, results = LoopRunner(self.config, engine).run(
            label="WAL 001", hrid="TEST", cycles=2, pause_seconds=0.0
        )
        data_path, report_path = LoopReportWriter(self.config.validation_path).write(summary, results)
        self.assertTrue(data_path.exists())
        self.assertTrue(report_path.exists())
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("RAPORT WALIDACYJNY", report)
        self.assertIn("POWTARZALNOŚĆ STANOWISKA", report)
        rows = data_path.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(len(rows), 3)          # nagłówek + 2 cykle
        self.assertTrue(rows[0].startswith("Cykl;"))

    def test_drift_is_detected(self) -> None:
        """Rosnące napięcie w kolejnych cyklach ma dawać dodatni trend (mV/cykl)."""

        class DriftingHardware(FakeHardware):
            """Odwzorowuje nagrzewanie się wyrobu: każdy cykl o 10 mV wyżej."""

            def __init__(self) -> None:
                super().__init__(follow_profile=True)
                self.cycles = 0

            def set_profile(self, index: int, force: bool = False) -> bool:
                if index == 1:
                    self.cycles += 1
                    self.offset = 0.01 * self.cycles
                return super().set_profile(index, force)

        config = fast_config()
        config.profiles = [config.profiles[0]]
        engine = TestEngine(config, DriftingHardware())
        summary, _ = LoopRunner(config, engine).run(
            label="WAL", hrid="TEST", cycles=4, pause_seconds=0.0
        )
        drift = summary.phases[0].drift_mv_per_cycle
        self.assertGreater(drift, 5.0)


class StationTests(unittest.TestCase):
    """Konfiguracja wielokanałowa i ochrona przed tą samą sztuką na dwóch gniazdach."""

    def test_single_station_by_default(self) -> None:
        config = AppConfig()
        self.assertEqual(len(config.active_stations()), 1)
        self.assertFalse(config.multi_station)
        self.assertEqual(config.validate(), [])

    def test_two_stations_need_serial_numbers(self) -> None:
        config = AppConfig()
        config.stations = [StationConfig("LEWY"), StationConfig("PRAWY")]
        errors = config.validate()
        self.assertTrue(any("numer seryjny" in item for item in errors), errors)

    def test_two_stations_with_serials_are_valid(self) -> None:
        config = AppConfig()
        config.stations = [
            StationConfig("LEWY", "PMPD80LLAD"),
            StationConfig("PRAWY", "PMPD80LN6E"),
        ]
        self.assertEqual(config.validate(), [])
        self.assertTrue(config.multi_station)

    def test_duplicate_serial_is_rejected(self) -> None:
        config = AppConfig()
        config.stations = [
            StationConfig("LEWY", "PMPD80LLAD"),
            StationConfig("PRAWY", "PMPD80LLAD"),
        ]
        self.assertTrue(any("Powtórzony numer" in item for item in config.validate()))

    def test_legacy_config_migrates_to_single_station(self) -> None:
        """Stara konfiguracja z device_serial ma dać jedno stanowisko."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test_config.json"
            payload = AppConfig().to_payload()
            payload.pop("stations")
            payload["device_serial"] = "PMPD80LLAD"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config, warnings = AppConfig.load(path)
        self.assertEqual(len(config.stations), 1)
        self.assertEqual(config.stations[0].device_serial, "PMPD80LLAD")
        self.assertTrue(any("jednokanałowa" in item for item in warnings))

    def test_serial_cannot_run_on_two_stations_at_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = AttemptStore(Path(folder) / "attempts.json")
            self.assertIsNone(store.reserve("SN00001", "LEWY"))
            self.assertEqual(store.reserve("SN00001", "PRAWY"), "LEWY")
            store.release("SN00001", "LEWY")
            self.assertIsNone(store.reserve("SN00001", "PRAWY"))

    def test_result_carries_station_name(self) -> None:
        config = fast_config()
        result = TestEngine(config, FakeHardware(follow_profile=True)).run(
            "TEST", "SN00001", station="PRAWY"
        )
        self.assertEqual(result.station, "PRAWY")
        headers = TestResult.csv_headers(result.profile_names)
        self.assertEqual(headers[1], "Stanowisko")
        self.assertEqual(result.to_csv_row()[1], "PRAWY")

    def test_precheck_uses_profile_count(self) -> None:
        """Pre-kontrola pyta tester o liczbę profili PD zamiast mierzyć napięcie."""
        config = fast_config()
        hardware = FakeHardware(voltage=0.0)
        detected, _voltage, message = TestEngine(config, hardware).detect_dut()
        self.assertFalse(detected)
        self.assertIn("profil", message.lower())


class DetectedStationTests(unittest.TestCase):
    """Automatyczna konfiguracja kanałów z wykrytych testerów."""

    def test_names_follow_channel_count(self) -> None:
        self.assertEqual(station_names_for(1), ["STANOWISKO"])
        self.assertEqual(station_names_for(2), ["LEWY", "PRAWY"])
        self.assertEqual(len(station_names_for(3)), 3)

    def test_stations_built_from_find_output(self) -> None:
        stations = stations_from_devices([(0, "PMPD80LLAD"), (1, "PMPD80LN6E")])
        self.assertEqual([item.name for item in stations], ["LEWY", "PRAWY"])
        self.assertEqual([item.device_serial for item in stations], ["PMPD80LLAD", "PMPD80LN6E"])
        config = AppConfig()
        config.stations = stations
        self.assertEqual(config.validate(), [])
        self.assertTrue(config.multi_station)


class RemapTests(unittest.TestCase):
    """Przypisanie wykrytych testerów do istniejących gniazd (przycisk WYKRYJ TESTERY)."""

    @staticmethod
    def _two_stations() -> list[StationConfig]:
        return [StationConfig("LEWY", "AAA"), StationConfig("PRAWY", "BBB")]

    def test_matching_serials_give_no_changes(self) -> None:
        plan = plan_station_remap(self._two_stations(), [(0, "AAA"), (1, "BBB")])
        self.assertFalse(plan.has_changes)
        self.assertEqual(plan.unchanged, ["LEWY", "PRAWY"])
        self.assertEqual(plan.missing, [])

    def test_changed_serial_is_remapped_keeping_names(self) -> None:
        """Przypadek z produkcji: prawy tester zgłosił się pod innym numerem."""
        plan = plan_station_remap(self._two_stations(), [(0, "AAA"), (1, "CCC")])
        self.assertEqual([item.name for item in plan.stations], ["LEWY", "PRAWY"])
        self.assertEqual([item.device_serial for item in plan.stations], ["AAA", "CCC"])
        self.assertEqual(plan.unchanged, ["LEWY"])
        self.assertEqual(len(plan.changes), 1)
        self.assertFalse(plan.requires_restart)

    def test_order_of_detection_does_not_steal_matched_serial(self) -> None:
        """Dopasowanie istniejących numerów idzie przed przypisaniem wolnych."""
        plan = plan_station_remap(self._two_stations(), [(0, "CCC"), (1, "BBB")])
        self.assertEqual([item.device_serial for item in plan.stations], ["CCC", "BBB"])
        self.assertEqual(plan.unchanged, ["PRAWY"])

    def test_missing_station_is_reported_not_silently_dropped(self) -> None:
        plan = plan_station_remap(self._two_stations(), [(0, "AAA")])
        self.assertEqual(plan.missing, ["PRAWY"])
        self.assertEqual(len(plan.stations), 2)
        self.assertFalse(plan.has_changes)

    def test_extra_tester_adds_station_and_needs_restart(self) -> None:
        plan = plan_station_remap([StationConfig("STANOWISKO", "AAA")], [(0, "AAA"), (1, "BBB")])
        self.assertEqual(len(plan.stations), 2)
        self.assertTrue(plan.requires_restart)
        self.assertEqual(plan.added, ["PRAWY"])

    def test_single_station_with_any_address_takes_detected_serial(self) -> None:
        plan = plan_station_remap([StationConfig("STANOWISKO", None)], [(0, "AAA")])
        self.assertEqual(plan.stations[0].device_serial, "AAA")
        self.assertTrue(plan.has_changes)

    def test_disabled_station_is_passed_through(self) -> None:
        stations = [StationConfig("LEWY", "AAA"), StationConfig("ZAPAS", "ZZZ", False)]
        plan = plan_station_remap(stations, [(0, "CCC")])
        self.assertEqual(plan.stations[0].device_serial, "CCC")
        self.assertEqual(plan.stations[1], stations[1])

    def test_result_is_a_valid_configuration(self) -> None:
        config = AppConfig()
        config.stations = self._two_stations()
        plan = plan_station_remap(config.stations, [(0, "DDD"), (1, "EEE")])
        config.stations = plan.stations
        self.assertEqual(config.validate(), [])

    def test_duplicate_detection_entries_are_ignored(self) -> None:
        plan = plan_station_remap(self._two_stations(), [(0, "AAA"), (1, "AAA")])
        self.assertEqual(plan.missing, ["PRAWY"])
        self.assertEqual(plan.stations[0].device_serial, "AAA")


class ReturnCodeTests(unittest.TestCase):
    def test_device_lost_codes(self) -> None:
        from hardware import DEVICE_LOST_CODES, describe_code

        self.assertIn(2, DEVICE_LOST_CODES)
        self.assertIn(3, DEVICE_LOST_CODES)
        self.assertNotIn(1, DEVICE_LOST_CODES)
        self.assertNotIn(4, DEVICE_LOST_CODES)
        self.assertIn("firmware", describe_code(6))

    def test_connection_status_is_about_dut(self) -> None:
        """STATUS:NOT CONNECTED z kodem 0 to sprawny tester z pustym gniazdem."""
        client = PM125Client("dowolny.exe", "PMPD80LLAD", 1.0)
        with patch.object(PM125Client, "_run", return_value="STATUS:NOT CONNECTED"), \
                patch("pathlib.Path.is_file", return_value=True):
            self.assertTrue(client.connect())
        self.assertTrue(client.connected)
        self.assertFalse(client._dut_in_output("STATUS:NOT CONNECTED"))
        self.assertTrue(client._dut_in_output("STATUS:CONNECTED"))

    def test_find_devices_parses_console_output(self) -> None:
        from hardware import find_devices

        with patch("pathlib.Path.is_file", return_value=True), \
                patch("subprocess.run") as runner:
            runner.return_value.stdout = "0=PMPD80LLAD\n1=PMPD80LN6E\n"
            devices = find_devices("dowolny.exe")
        self.assertEqual(devices, [(0, "PMPD80LLAD"), (1, "PMPD80LN6E")])


def _tk_available() -> bool:
    """Testy okien mają sens tylko tam, gdzie jest Tkinter i pulpit (stacja z Pythonem)."""
    try:
        import tkinter

        root = tkinter.Tk()
    except Exception:
        return False
    root.destroy()
    return True


TK_AVAILABLE = _tk_available()


@unittest.skipUnless(TK_AVAILABLE, "brak Tkinter albo pulpitu")
class GuiSmokeTests(unittest.TestCase):
    """Budowa okien. Łapie literówki w konstruktorach, których logika sama nie wykryje."""

    def setUp(self) -> None:
        import tkinter as tk

        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        base = Path(self.folder.name)
        self.config = AppConfig()
        for attribute, name in (
            ("reports_directory", "reports"), ("backup_directory", "backup"),
            ("logs_directory", "logs"), ("raw_directory", "raw"),
            ("validation_directory", "validation"),
        ):
            setattr(self.config, attribute, str(base / name))
        self.config.attempts_file = str(base / "attempts.json")
        self.config.pending_file = str(base / "pending.json")
        self.config.audit_file = str(base / "audit.log")
        self.config.health_check_seconds = 0
        self.config_path = base / "config.json"
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        import tkinter as tk

        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _two_station_app(self):
        import ui

        self.config.stations = [
            StationConfig("LEWY", "AAA"), StationConfig("PRAWY", "BBB")
        ]
        self.config.save(self.config_path)
        hardware = ui.build_hardware(self.config, True, "pass", 0.0)
        app = ui.TesterApp(
            self.root, self.config, hardware, self.config_path, simulation_mode=True
        )
        self.root.update()
        return app

    def test_loop_window_builds(self) -> None:
        """Regresja: okno serii odwoływało się do nieistniejącej nazwy i nie otwierało się."""
        from loop_ui import LoopWindow

        window = LoopWindow(
            self.root, self.config, TestEngine(self.config, SimulatedPM125("pass")),
            None, "TEST", on_state_change=lambda _running: None, station_name="LEWY",
        )
        self.root.update()
        self.assertTrue(window.exists())
        window.window.destroy()

    def test_loop_windows_open_for_every_station(self) -> None:
        app = self._two_station_app()
        app._open_loop_window()
        self.root.update()
        self.assertIsNotNone(app._loop_chooser)
        app._launch_all_loops()
        self.root.update()
        self.assertEqual(sorted(app._loop_windows), ["LEWY", "PRAWY"])
        self.assertTrue(all(window.exists() for window in app._loop_windows.values()))

    def test_validation_blocks_only_its_own_station(self) -> None:
        """Wymaganie: seria na jednym gnieździe nie może zatrzymywać produkcji na drugim."""
        app = self._two_station_app()
        app.operator_hrid = "TEST"
        app._loop_state_changed("LEWY", True)
        self.root.update()
        self.assertTrue(app.panels[0].loop_active)
        self.assertFalse(app.panels[1].loop_active)
        self.assertFalse(app.blocked)
        app._loop_state_changed("LEWY", False)
        self.assertFalse(app.panels[0].loop_active)

    def test_detect_button_exists_in_operator_bar(self) -> None:
        app = self._two_station_app()
        self.assertEqual(app.detect_button.cget("text"), "WYKRYJ TESTERY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
