from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core.exceptions import ConfigurationError  # noqa: E402
from job_application_automation.core.runtime_config import (  # noqa: E402
    DEFAULT_RUNTIME_CONFIG_DIR,
    RUNTIME_CONFIG_DIR,
    RUNTIME_SECTION_NAMES,
    RuntimeConfig,
    load_runtime_config,
    resolve_runtime_path,
)


def _split_document(directory: Path) -> dict[str, object]:
    document = json.loads((directory / "schema_version.json").read_text(encoding="utf-8"))
    for section_name in RUNTIME_SECTION_NAMES:
        section_document = json.loads(
            (directory / f"{section_name}.json").read_text(encoding="utf-8")
        )
        document.update(section_document)
    return document


def _canonical_json(document: object) -> str:
    return json.dumps(document, allow_nan=False, separators=(",", ":"), sort_keys=True)


LEGACY_ASHBY_WORKER_SETTINGS = {
    "continuous_sleep_min_seconds": 900,
    "continuous_sleep_max_seconds": 1_800,
    "continuous_application_limit": 12,
    "continuous_application_window_seconds": 86_400,
    "spam_rejection_cooldown_seconds": 86_400,
    "spam_rejection_threshold": 1,
}


def _inject_legacy_ashby_worker_settings(document: dict[str, object]) -> None:
    document["ashby"].update(LEGACY_ASHBY_WORKER_SETTINGS)


class RuntimeConfigTests(unittest.TestCase):
    def test_packaged_defaults_match_the_tracked_runtime_config(self) -> None:
        self.assertEqual(
            _split_document(DEFAULT_RUNTIME_CONFIG_DIR), _split_document(RUNTIME_CONFIG_DIR)
        )
        self.assertEqual(
            {path.name for path in DEFAULT_RUNTIME_CONFIG_DIR.glob("*.json")},
            {path.name for path in RUNTIME_CONFIG_DIR.glob("*.json")},
        )

    def test_tracked_runtime_config_loads_all_shared_operational_settings(self) -> None:
        config = load_runtime_config()

        self.assertEqual(config.browser.cdp_endpoint, "http://localhost:9222")
        self.assertEqual(config.vertex.project_id, "from-service-account")
        self.assertGreater(config.ashby.max_submit_attempts, 0)
        ashby_worker = config.continuous_worker.for_provider("ashby")
        self.assertGreaterEqual(ashby_worker.sleep_min_seconds, 900)
        self.assertGreaterEqual(
            ashby_worker.sleep_max_seconds,
            ashby_worker.sleep_min_seconds,
        )
        self.assertEqual(ashby_worker.application_limit, 12)
        self.assertEqual(ashby_worker.application_window_seconds, 86_400)
        self.assertEqual(ashby_worker.spam_rejection_cooldown_seconds, 86_400)
        self.assertEqual(ashby_worker.spam_rejection_threshold, 1)
        self.assertEqual(config.ashby.submission_result_timeout_seconds, 15)
        self.assertEqual(config.ashby.submission_result_poll_seconds, 0.5)
        self.assertGreater(config.gmail.greenhouse_security_code_poll_timeout_seconds, 30)
        self.assertGreater(config.resume.original_character_count, 0)
        self.assertIn("greenhouse", config.search.ats_hosts)
        self.assertIn("AI", config.search.ai_terms)
        self.assertEqual(config.search.defaults.max_discovery_queries, 400)
        self.assertEqual(config.application.vps_max_document_jobs, 10)
        self.assertEqual(config.application.vps_document_retry_jobs, 2)
        self.assertEqual(config.application.vps_max_attempts_per_ats, 10)
        self.assertEqual(
            config.application.vps_application_state_file,
            "output/vps_application_state.json",
        )
        self.assertEqual(
            config.application.vps_application_failure_report,
            "output/vps_application_failures.json",
        )
        self.assertEqual(
            config.application.vps_job_backlog_file,
            "output/job_backlog.json",
        )
        self.assertEqual(
            resolve_runtime_path(config.application.resume_source_file),
            ROOT / "data" / "base_resume.txt",
        )
        self.assertEqual(
            resolve_runtime_path(config.application.seo_config_file),
            ROOT / "config" / "seo_config.json",
        )
        self.assertEqual(config.application["queue_timeout_seconds"], 300)
        self.assertEqual(config.application.get("queue_timeout_seconds"), 300)

    def test_typed_models_round_trip_and_are_frozen(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        config = RuntimeConfig.from_mapping(document)

        self.assertEqual(config.to_mapping(), document)
        self.assertEqual(_canonical_json(config.to_mapping()), _canonical_json(document))
        self.assertIs(type(config.vertex.retry_delay_seconds), int)
        self.assertIs(type(config.resume.original_page_height), int)
        self.assertIs(type(config.search.defaults.timeout_seconds), int)
        self.assertIs(type(config.search.defaults.delay_seconds), float)
        self.assertIs(type(config.observability.flush_timeout_seconds), float)
        self.assertEqual(RuntimeConfig.from_mapping(config.to_mapping()), config)
        with self.assertRaises(FrozenInstanceError):
            config.application.queue_timeout_seconds = 1  # type: ignore[misc]

    def test_number_fields_preserve_explicit_integer_and_float_json_types(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document["vertex"]["retry_delay_seconds"] = 2.0
        document["search"]["defaults"]["timeout_seconds"] = 20
        document["search"]["defaults"]["async_timeout_seconds"] = 10.0

        serialized = RuntimeConfig.from_mapping(document).to_mapping()

        self.assertEqual(_canonical_json(serialized), _canonical_json(document))
        self.assertIs(type(serialized["vertex"]["retry_delay_seconds"]), float)
        self.assertIs(type(serialized["search"]["defaults"]["timeout_seconds"]), int)
        self.assertIs(type(serialized["search"]["defaults"]["async_timeout_seconds"]), float)

    def test_schema_version_rejects_boolean_and_float_lookalikes(self) -> None:
        for invalid_version in (True, 1.0):
            with self.subTest(invalid_version=invalid_version):
                document = _split_document(RUNTIME_CONFIG_DIR)
                document["schema_version"] = invalid_version

                with self.assertRaisesRegex(ConfigurationError, "integer 1"):
                    RuntimeConfig.from_mapping(document)

    def test_non_finite_number_is_rejected(self) -> None:
        for invalid_number in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid_number=invalid_number):
                document = _split_document(RUNTIME_CONFIG_DIR)
                document["vertex"]["retry_delay_seconds"] = invalid_number

                with self.assertRaisesRegex(ConfigurationError, "non-negative number"):
                    RuntimeConfig.from_mapping(document)

    def test_unknown_and_missing_keys_are_rejected_by_exact_section(self) -> None:
        unknown = _split_document(RUNTIME_CONFIG_DIR)
        unknown["application"]["queue_timout_seconds"] = 300
        with self.assertRaisesRegex(ConfigurationError, "application.*unknown keys"):
            RuntimeConfig.from_mapping(unknown)

        missing = _split_document(RUNTIME_CONFIG_DIR)
        missing["gmail"].pop("token_file")
        with self.assertRaisesRegex(ConfigurationError, "gmail.*missing required keys.*token_file"):
            RuntimeConfig.from_mapping(missing)

    def test_provider_overrides_merge_with_typed_worker_defaults(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document["continuous_worker"]["providers"]["greenhouse"] = {
            "sleep_min_seconds": 17,
            "application_limit": 4,
        }

        config = RuntimeConfig.from_mapping(document)
        greenhouse = config.continuous_worker.for_provider("greenhouse")
        lever = config.continuous_worker.for_provider("lever")

        self.assertEqual(greenhouse.sleep_min_seconds, 17)
        self.assertEqual(greenhouse.sleep_max_seconds, 300)
        self.assertEqual(greenhouse.application_limit, 4)
        self.assertEqual(lever.sleep_min_seconds, 120)
        self.assertEqual(
            config.continuous_worker["providers"]["greenhouse"],
            {
                "sleep_min_seconds": 17,
                "application_limit": 4,
            },
        )

    def test_sparse_provider_pacing_is_validated_against_inherited_defaults(self) -> None:
        invalid_overrides = (
            ({"sleep_min_seconds": 301}, "sleep_min_seconds"),
            ({"sleep_max_seconds": 100}, "sleep_min_seconds"),
        )
        for override, expected_field in invalid_overrides:
            with self.subTest(override=override):
                document = _split_document(RUNTIME_CONFIG_DIR)
                document["continuous_worker"]["providers"]["greenhouse"] = override

                with self.assertRaisesRegex(
                    ConfigurationError,
                    rf"continuous_worker\.providers\.greenhouse\.{expected_field}",
                ):
                    RuntimeConfig.from_mapping(document)

    def test_search_defaults_accept_only_cli_supported_choices(self) -> None:
        allowed_values = {
            "discovery_mode": ("focused", "expanded", "exhaustive"),
            "discovery_timelimit": ("auto", "none"),
            "match_mode": ("strict", "expanded"),
            "scrape_discovered_pages": ("none", "failed-feed", "all"),
            "live_check_target": ("listing", "application", "both"),
        }
        for field, choices in allowed_values.items():
            for choice in choices:
                with self.subTest(field=field, choice=choice):
                    document = _split_document(RUNTIME_CONFIG_DIR)
                    document["search"]["defaults"][field] = choice
                    self.assertEqual(
                        getattr(RuntimeConfig.from_mapping(document).search.defaults, field),
                        choice,
                    )

            document = _split_document(RUNTIME_CONFIG_DIR)
            document["search"]["defaults"][field] = "unsupported"
            with self.subTest(field=field, choice="unsupported"):
                with self.assertRaisesRegex(
                    ConfigurationError,
                    rf"search\.defaults\.{field}",
                ):
                    RuntimeConfig.from_mapping(document)

    def test_search_backend_must_be_configured_or_intentional_all(self) -> None:
        for backend in (*_split_document(RUNTIME_CONFIG_DIR)["search"]["ddgs_backends"], "all"):
            with self.subTest(backend=backend):
                document = _split_document(RUNTIME_CONFIG_DIR)
                document["search"]["defaults"]["search_backend"] = backend
                self.assertEqual(
                    RuntimeConfig.from_mapping(document).search.defaults.search_backend,
                    backend,
                )

        document = _split_document(RUNTIME_CONFIG_DIR)
        document["search"]["defaults"]["search_backend"] = "unsupported"
        with self.assertRaisesRegex(ConfigurationError, "search.defaults.search_backend"):
            RuntimeConfig.from_mapping(document)

    def test_role_family_aliases_must_reference_a_defined_family(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document["search"]["role_family_input_aliases"]["missing-family"] = ["missing"]

        with self.assertRaisesRegex(ConfigurationError, "undefined role_families.*missing-family"):
            RuntimeConfig.from_mapping(document)

    def test_legacy_schema_one_document_gains_behavior_preserving_typed_sections(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document.pop("observability")
        document.pop("continuous_worker")
        _inject_legacy_ashby_worker_settings(document)

        config = RuntimeConfig.from_mapping(document)

        self.assertEqual(config.observability.default_environment, "production")
        self.assertEqual(config.continuous_worker.source.sleep_min_seconds, 5)
        self.assertEqual(
            config.continuous_worker.source.engine_timeout_seconds,
            config.application.queue_timeout_seconds,
        )
        self.assertEqual(
            config.continuous_worker.for_provider("ashby").sleep_min_seconds,
            900,
        )

    def test_legacy_and_explicit_ashby_worker_values_must_not_conflict(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        _inject_legacy_ashby_worker_settings(document)
        document["ashby"]["continuous_sleep_min_seconds"] = 901

        with self.assertRaisesRegex(
            ConfigurationError,
            "ashby.continuous_sleep_min_seconds.*"
            "continuous_worker.providers.ashby.sleep_min_seconds",
        ):
            RuntimeConfig.from_mapping(document)

    def test_identical_legacy_and_explicit_ashby_worker_values_remain_compatible(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        _inject_legacy_ashby_worker_settings(document)

        config = RuntimeConfig.from_mapping(document)

        self.assertEqual(config.continuous_worker.for_provider("ashby").sleep_min_seconds, 900)

    def test_missing_worker_sections_and_legacy_keys_use_generic_defaults(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document.pop("continuous_worker")

        config = RuntimeConfig.from_mapping(document)

        self.assertEqual(config.continuous_worker.for_provider("ashby").sleep_min_seconds, 120)

    def test_representative_consumers_receive_typed_attribute_defaults(self) -> None:
        from job_application_automation.core import (
            continuous_ats,
            continuous_source_ats,
            continuous_worker_application,
            orchestrator,
        )
        from job_application_automation.engines import browser_runtime
        from job_application_automation.search import config as search_config

        config = load_runtime_config()

        self.assertEqual(
            orchestrator.DEFAULT_ENGINE_TIMEOUT_SECONDS,
            config.application.engine_timeout_seconds,
        )
        self.assertEqual(
            continuous_ats.build_parser("ashby").parse_args(["--once"]).sleep_min_seconds,
            config.continuous_worker.for_provider("ashby").sleep_min_seconds,
        )
        direct_args = continuous_ats.build_parser("greenhouse").parse_args(["--once"])
        direct_defaults = config.continuous_worker.for_provider("greenhouse")
        self.assertEqual(direct_args.sleep_min_seconds, direct_defaults.sleep_min_seconds)
        self.assertEqual(direct_args.sleep_max_seconds, direct_defaults.sleep_max_seconds)
        self.assertEqual(
            direct_args.document_timeout_seconds,
            direct_defaults.document_timeout_seconds,
        )
        self.assertEqual(direct_args.engine_timeout_seconds, direct_defaults.engine_timeout_seconds)
        self.assertEqual(
            direct_args.application_timeout_seconds,
            direct_defaults.application_timeout_seconds,
        )
        source_args = continuous_source_ats.build_parser().parse_args(
            [
                "--ats-platform",
                "greenhouse",
                "--source",
                "search",
                "--worker-id",
                "config-test",
                "--state",
                "state.json",
                "--selected-input",
                "selected.json",
                "--results-dir",
                "results",
                "--documents-dir",
                "documents",
                "--once",
            ]
        )
        self.assertEqual(
            source_args.sleep_min_seconds,
            config.continuous_worker.source.sleep_min_seconds,
        )
        self.assertEqual(
            source_args.engine_timeout_seconds,
            config.continuous_worker.source.engine_timeout_seconds,
        )
        self.assertEqual(
            source_args.application_timeout_seconds,
            config.continuous_worker.source.application_timeout_seconds,
        )
        self.assertEqual(
            continuous_worker_application.DEFAULT_SUBMISSION_LOG,
            resolve_runtime_path(config.application.submission_log_file),
        )
        self.assertEqual(
            continuous_worker_application.DEFAULT_BACKLOG,
            resolve_runtime_path(config.application.vps_job_backlog_file),
        )
        self.assertEqual(
            continuous_worker_application.DEFAULT_EMAIL_POOL,
            resolve_runtime_path(config.application.candidate_email_pool_file),
        )
        self.assertEqual(
            browser_runtime.RUNTIME_CONFIG.browser.cdp_endpoint,
            config.browser.cdp_endpoint,
        )
        self.assertEqual(
            search_config.DEFAULTS.results_per_query,
            config.search.defaults.results_per_query,
        )

    def test_invalid_runtime_setting_is_rejected_before_workflow_startup(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document["ashby"]["max_form_steps"] = 0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ashby.max_form_steps"):
                load_runtime_config(path)

    def test_invalid_search_setting_is_rejected_before_workflow_startup(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document["search"]["defaults"]["results_per_query"] = 0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "search.defaults.results_per_query"):
                load_runtime_config(path)

    def test_older_schema_one_config_remains_valid_without_new_ashby_controls(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        for key in (
            "continuous_sleep_min_seconds",
            "continuous_sleep_max_seconds",
            "continuous_application_limit",
            "continuous_application_window_seconds",
            "spam_rejection_cooldown_seconds",
            "spam_rejection_threshold",
            "submission_result_timeout_seconds",
            "submission_result_poll_seconds",
            "submission_spam_phrases",
        ):
            document["ashby"].pop(key, None)
        document["browser"]["cdp_endpoint"] = "http://127.0.0.1:9333"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            config = load_runtime_config(path)

        self.assertEqual(config.browser["cdp_endpoint"], "http://127.0.0.1:9333")
        self.assertNotIn("continuous_application_limit", config.ashby)

    def test_legacy_zero_continuous_application_limit_disables_the_optional_cap(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document.pop("continuous_worker")
        document["ashby"]["continuous_application_limit"] = 0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            config = load_runtime_config(path)

        self.assertEqual(config.ashby["continuous_application_limit"], 0)
        self.assertEqual(config.continuous_worker.for_provider("ashby").application_limit, 0)

    def test_cover_letter_section_loads_with_valid_word_budget(self) -> None:
        config = load_runtime_config()

        self.assertGreater(config.cover_letter["max_retries"], 0)
        self.assertGreater(
            config.cover_letter["maximum_words"], config.cover_letter["minimum_words"]
        )

    def test_cover_letter_word_budget_must_be_ordered(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document["cover_letter"]["maximum_words"] = document["cover_letter"]["minimum_words"]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "maximum_words"):
                load_runtime_config(path)

    def test_split_config_rejects_missing_section_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            split_dir = Path(directory)
            for source in RUNTIME_CONFIG_DIR.glob("*.json"):
                if source.name != "gmail.json":
                    (split_dir / source.name).write_text(
                        source.read_text(encoding="utf-8"), encoding="utf-8"
                    )

            with self.assertRaisesRegex(ValueError, "missing: gmail.json"):
                load_runtime_config(split_dir)

    def test_schema_one_split_config_accepts_omitted_new_optional_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            split_dir = Path(directory)
            for source in RUNTIME_CONFIG_DIR.glob("*.json"):
                if source.stem not in {"observability", "continuous_worker"}:
                    (split_dir / source.name).write_text(
                        source.read_text(encoding="utf-8"), encoding="utf-8"
                    )
            ashby_path = split_dir / "ashby.json"
            ashby_document = json.loads(ashby_path.read_text(encoding="utf-8"))
            _inject_legacy_ashby_worker_settings(ashby_document)
            ashby_path.write_text(json.dumps(ashby_document), encoding="utf-8")

            config = load_runtime_config(split_dir)

        self.assertEqual(config.observability.flush_timeout_seconds, 2.0)
        self.assertEqual(config.continuous_worker.for_provider("ashby").sleep_min_seconds, 900)

    def test_present_invalid_checkout_config_never_falls_back_to_packaged_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            split_dir = Path(directory) / "runtime"
            split_dir.mkdir()
            (split_dir / "schema_version.json").write_text("not-json", encoding="utf-8")

            with (
                patch(
                    "job_application_automation.core.runtime_config.RUNTIME_CONFIG_DIR",
                    split_dir,
                ),
                self.assertRaisesRegex(ValueError, "runtime config directory is missing"),
            ):
                load_runtime_config()

    def test_explicit_legacy_monolithic_config_remains_supported(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            config = load_runtime_config(path)

        self.assertEqual(config.browser["cdp_endpoint"], "http://localhost:9222")

    def test_invalid_utf8_is_reported_as_a_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_bytes(b'\xff{"schema_version": 1}')

            with self.assertRaisesRegex(
                ConfigurationError,
                "runtime config contains invalid JSON or cannot be read",
            ):
                load_runtime_config(path)


if __name__ == "__main__":
    unittest.main()
