from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
BASH = next(
    (
        candidate
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else "",
            shutil.which("bash") or "",
        )
        if candidate and Path(candidate).is_file()
    ),
    None,
)

from job_application_automation.core import document_cli  # noqa: E402
from job_application_automation.core import document_archive  # noqa: E402
from job_application_automation.core.adapters import (  # noqa: E402
    CommandResult,
    ProcessSettings,
)
from job_application_automation.core.document_archive import (  # noqa: E402
    COVER_LETTER_STORED_NAME,
    MANIFEST_STORED_NAME,
    RESUME_STORED_NAME,
    ArchiveConflictError,
    ArchiveIntegrityError,
    ArchiveKey,
    ArchiveNotFoundError,
    DocumentArchiveError,
    PuttyArchiveTransport,
    VpsArchiveConfig,
    build_store_plan,
    execute_store,
    retrieve_archive,
)
from job_application_automation.core.identity import (  # noqa: E402
    canonical_job_url,
    normalize_email,
)


def _write_pdf(path: Path, content: bytes) -> Path:
    path.write_bytes(b"%PDF-1.4\n" + content)
    return path


def _key(**overrides: str) -> ArchiveKey:
    values = {
        "job_url": "https://jobs.example.com/openings/123?utm_source=mail",
        "company": "Example Company",
        "job_title": "Product Manager",
        "email_used": "Candidate@Example.com",
    }
    values.update(overrides)
    return ArchiveKey(**values)


class MemoryArchiveTransport:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, bytes]] = {}
        self.download_calls = 0
        self.corrupt_resume = False

    def remote_record_path(self, archive_id: str) -> str:
        return f"/private/records/{archive_id}"

    def store_record(self, plan) -> str:
        payload = {
            MANIFEST_STORED_NAME: json.dumps(
                plan.manifest.to_payload(),
                sort_keys=True,
            ).encode(),
            RESUME_STORED_NAME: plan.resume_path.read_bytes(),
            COVER_LETTER_STORED_NAME: plan.cover_letter_path.read_bytes(),
        }
        existing = self.records.get(plan.archive_id)
        if existing is not None:
            existing_manifest = json.loads(existing[MANIFEST_STORED_NAME])
            if existing_manifest["record_fingerprint"] != plan.manifest.record_fingerprint:
                raise ArchiveConflictError("conflicting immutable record")
            return "ALREADY_STORED"
        self.records[plan.archive_id] = payload
        return "STORED"

    def download_record(self, archive_id: str, destination: Path) -> None:
        self.download_calls += 1
        record = self.records.get(archive_id)
        if record is None:
            raise ArchiveNotFoundError("not found")
        for name, content in record.items():
            if self.corrupt_resume and name == RESUME_STORED_NAME:
                content += b"tampered"
            (destination / name).write_bytes(content)


class RecordingRunner:
    def __init__(self, *, conflict: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.password_files: list[Path] = []
        self.password_contents: list[str] = []
        self.conflict = conflict

    def run(self, command: list[str], settings: ProcessSettings) -> CommandResult:
        del settings
        copied = list(command)
        self.commands.append(copied)
        if "-pwfile" in copied:
            password_path = Path(copied[copied.index("-pwfile") + 1])
            self.password_files.append(password_path)
            self.password_contents.append(password_path.read_text(encoding="utf-8"))
        remote_command = copied[-1] if copied[0] == "plink-test" else ""
        if "printf STORED" in remote_command:
            if self.conflict:
                return CommandResult(73, stderr="ARCHIVE_CONFLICT")
            return CommandResult(0, stdout="STORED")
        return CommandResult(0)


class FailingRunner:
    def __init__(self, diagnostics: str) -> None:
        self.diagnostics = diagnostics

    def run(self, command: list[str], settings: ProcessSettings) -> CommandResult:
        del command, settings
        return CommandResult(1, stderr=self.diagnostics)


class IdentityTests(unittest.TestCase):
    def test_url_aliases_and_email_case_produce_the_same_opaque_id(self) -> None:
        first = _key()
        second = _key(
            job_url="HTTPS://JOBS.EXAMPLE.COM:443/openings/123/?source=queue#apply",
            company="Renamed display value",
            job_title="Different display value",
            email_used="candidate@example.COM",
        )

        self.assertEqual(
            canonical_job_url(first.job_url),
            "https://jobs.example.com/openings/123",
        )
        self.assertEqual(first.archive_id, second.archive_id)
        self.assertRegex(first.archive_id, r"^ja1_[0-9a-f]{64}$")
        self.assertNotIn("candidate", first.archive_id)
        self.assertNotIn("example-company", first.archive_id)

    def test_identity_preserves_non_tracking_query_parameters(self) -> None:
        self.assertEqual(
            canonical_job_url("https://jobs.example.com/embed?utm_medium=x&token=123&for=acme"),
            "https://jobs.example.com/embed?for=acme&token=123",
        )

    def test_invalid_url_and_email_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            canonical_job_url("http://jobs.example.com/123")
        with self.assertRaises(ValueError):
            canonical_job_url("https://user:secret@jobs.example.com/123")
        for value in ("candidate@", "@example.com", "candidate example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_email(value)


class ArchiveServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.resume = _write_pdf(self.root / "Candidate CV.pdf", b"resume")
        self.cover = _write_pdf(self.root / "Candidate Cover Letter.pdf", b"cover")
        self.clock = lambda: datetime(2026, 7, 28, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _plan(self, key: ArchiveKey | None = None):
        return build_store_plan(
            key or _key(),
            self.resume,
            self.cover,
            clock=self.clock,
        )

    def test_plan_hashes_both_pdfs_and_uses_only_a_relative_opaque_path(self) -> None:
        plan = self._plan()

        self.assertEqual(plan.manifest.created_at, "2026-07-28T00:00:00+00:00")
        self.assertEqual(plan.manifest.resume.original_filename, "Candidate CV.pdf")
        self.assertEqual(
            plan.relative_record_path,
            f"records/{plan.archive_id[4:6]}/{plan.archive_id}",
        )
        self.assertNotIn("Example", plan.relative_record_path)
        self.assertNotIn("@", plan.relative_record_path)

    def test_non_pdf_and_symbolic_link_inputs_are_rejected(self) -> None:
        text = self.root / "resume.txt"
        text.write_text("not a PDF", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be a PDF"):
            build_store_plan(_key(), text, self.cover)

        link = self.root / "resume-link.pdf"
        try:
            link.symlink_to(self.resume)
        except OSError:
            self.skipTest("symbolic links are unavailable in this environment")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            build_store_plan(_key(), link, self.cover)

    def test_store_is_idempotent_and_different_content_conflicts(self) -> None:
        transport = MemoryArchiveTransport()
        plan = self._plan()

        first = execute_store(plan, transport)
        second = execute_store(plan, transport)
        self.assertEqual(first.status, "STORED")
        self.assertEqual(second.status, "ALREADY_STORED")

        replacement_cover = _write_pdf(self.root / "replacement.pdf", b"different")
        conflict = build_store_plan(
            _key(),
            self.resume,
            replacement_cover,
            clock=self.clock,
        )
        with self.assertRaises(ArchiveConflictError):
            execute_store(conflict, transport)

    def test_retrieve_requires_all_four_selectors_and_verifies_hashes(self) -> None:
        transport = MemoryArchiveTransport()
        plan = self._plan()
        execute_store(plan, transport)
        destination = self.root / "retrieved"

        result = retrieve_archive(_key(), destination, transport)

        self.assertEqual(result.archive_id, plan.archive_id)
        self.assertEqual(result.resume_path.read_bytes(), self.resume.read_bytes())
        self.assertEqual(result.cover_letter_path.read_bytes(), self.cover.read_bytes())
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["archive_id"], plan.archive_id)

        mismatch_destination = self.root / "mismatch"
        with self.assertRaisesRegex(ArchiveNotFoundError, "company"):
            retrieve_archive(
                _key(company="Another Company"),
                mismatch_destination,
                transport,
            )
        self.assertFalse((mismatch_destination / RESUME_STORED_NAME).exists())

    def test_corrupt_download_never_replaces_existing_local_files(self) -> None:
        transport = MemoryArchiveTransport()
        execute_store(self._plan(), transport)
        destination = self.root / "retrieved"
        destination.mkdir()
        existing_resume = destination / RESUME_STORED_NAME
        existing_resume.write_bytes(b"keep me")
        transport.corrupt_resume = True

        with self.assertRaises(ArchiveIntegrityError):
            retrieve_archive(_key(), destination, transport, overwrite=True)

        self.assertEqual(existing_resume.read_bytes(), b"keep me")
        self.assertFalse((destination / COVER_LETTER_STORED_NAME).exists())

    def test_existing_destination_is_rejected_before_network_access(self) -> None:
        transport = MemoryArchiveTransport()
        destination = self.root / "retrieved"
        destination.mkdir()
        (destination / MANIFEST_STORED_NAME).write_text("existing", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            retrieve_archive(_key(), destination, transport)

        self.assertEqual(transport.download_calls, 0)

    def test_promotion_failure_restores_every_existing_archive_file(self) -> None:
        transport = MemoryArchiveTransport()
        execute_store(self._plan(), transport)
        destination = self.root / "retrieved"
        destination.mkdir()
        old_values = {
            RESUME_STORED_NAME: b"old resume",
            COVER_LETTER_STORED_NAME: b"old cover",
            MANIFEST_STORED_NAME: b"old manifest",
        }
        for name, content in old_values.items():
            (destination / name).write_bytes(content)

        real_replace = os.replace
        failed = False

        def fail_cover_promotion(source, target):
            nonlocal failed
            source_path = Path(source)
            if (
                not failed
                and source_path.name == COVER_LETTER_STORED_NAME
                and source_path.parent != destination
                and ".backup-" not in source_path.name
            ):
                failed = True
                raise OSError("simulated promotion failure")
            return real_replace(source, target)

        with (
            patch.object(document_archive.os, "replace", side_effect=fail_cover_promotion),
            self.assertRaisesRegex(OSError, "simulated promotion failure"),
        ):
            retrieve_archive(_key(), destination, transport, overwrite=True)

        for name, content in old_values.items():
            self.assertEqual((destination / name).read_bytes(), content)
        self.assertEqual(list(destination.glob(".*.backup-*")), [])


class PuttyTransportTests(unittest.TestCase):
    def test_config_requires_pinned_host_key_and_specific_private_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "ssh_host_key"):
            VpsArchiveConfig(
                host="archive.example.test",
                ssh_user="jobarchive",
                host_key="",
                archive_root="/srv/job-archive",
                password="password",
            )
        with self.assertRaisesRegex(ValueError, "specific absolute"):
            VpsArchiveConfig(
                host="archive.example.test",
                ssh_user="jobarchive",
                host_key="ssh-ed25519 255 SHA256:trusted",
                archive_root="/var/lib",
                password="password",
            )

    def test_upload_pins_host_key_hides_password_and_keeps_pii_out_of_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = _write_pdf(root / "Example Candidate Resume.pdf", b"resume")
            cover = _write_pdf(root / "Example Candidate Letter.pdf", b"cover")
            plan = build_store_plan(
                _key(),
                resume,
                cover,
                clock=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc),
            )
            runner = RecordingRunner()
            config = VpsArchiveConfig(
                host="archive.example.test",
                ssh_user="jobarchive",
                host_key="ssh-ed25519 255 SHA256:trusted-fingerprint",
                archive_root="/srv/job-archive",
                password="sentinel password",
            )
            transport = PuttyArchiveTransport(
                config,
                process_runner=runner,
                plink_path="plink-test",
                pscp_path="pscp-test",
            )

            result = execute_store(plan, transport)

        self.assertEqual(result.status, "STORED")
        self.assertGreaterEqual(len(runner.commands), 6)
        for command in runner.commands:
            self.assertIn("-hostkey", command)
            self.assertNotIn("sentinel password", command)
            joined = " ".join(command)
            self.assertNotIn("Candidate@Example.com", joined)
            self.assertNotIn("Example Company", joined)
            self.assertNotIn("Product Manager", joined)
            self.assertNotIn("Example Candidate Resume.pdf", joined)
        self.assertTrue(runner.password_files)
        self.assertEqual(set(runner.password_contents), {"sentinel password"})
        self.assertTrue(all(not path.exists() for path in runner.password_files))
        if BASH:
            for command in runner.commands:
                if command[0] != "plink-test":
                    continue
                parsed = subprocess.run(
                    [BASH, "-n", "-c", command[-1]],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(parsed.returncode, 0, parsed.stderr)

    def test_remote_immutable_conflict_is_classified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_store_plan(
                _key(),
                _write_pdf(root / "resume.pdf", b"resume"),
                _write_pdf(root / "letter.pdf", b"cover"),
            )
            runner = RecordingRunner(conflict=True)
            transport = PuttyArchiveTransport(
                VpsArchiveConfig(
                    host="archive.example.test",
                    ssh_user="jobarchive",
                    host_key="ssh-ed25519 255 SHA256:trusted-fingerprint",
                    archive_root="/srv/job-archive",
                    password="password",
                ),
                process_runner=runner,
                plink_path="plink-test",
                pscp_path="pscp-test",
            )

            with self.assertRaises(ArchiveConflictError):
                execute_store(plan, transport)

    def test_missing_record_is_distinct_from_connection_failure(self) -> None:
        config = VpsArchiveConfig(
            host="archive.example.test",
            ssh_user="jobarchive",
            host_key="ssh-ed25519 255 SHA256:trusted",
            archive_root="/srv/job-archive",
            password="password",
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            missing = PuttyArchiveTransport(
                config,
                process_runner=FailingRunner("ARCHIVE_NOT_FOUND"),
                plink_path="plink-test",
                pscp_path="pscp-test",
            )
            with self.assertRaises(ArchiveNotFoundError):
                missing.download_record(_key().archive_id, destination)

            unavailable = PuttyArchiveTransport(
                config,
                process_runner=FailingRunner("host key mismatch"),
                plink_path="plink-test",
                pscp_path="pscp-test",
            )
            with self.assertRaises(DocumentArchiveError) as failure:
                unavailable.download_record(_key().archive_id, destination)
            self.assertNotIsInstance(failure.exception, ArchiveNotFoundError)


class DocumentCliTests(unittest.TestCase):
    def test_store_is_offline_by_default_and_does_not_load_vps_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = _write_pdf(root / "resume.pdf", b"resume")
            cover = _write_pdf(root / "cover.pdf", b"cover")
            output = StringIO()
            errors = StringIO()
            arguments = [
                "store",
                "--url",
                "https://jobs.example.com/123",
                "--company",
                "Example",
                "--role",
                "PM",
                "--email",
                "candidate@example.com",
                "--resume",
                str(resume),
                "--cover-letter",
                str(cover),
                "--config",
                str(root / "does-not-exist.json"),
            ]

            with (
                patch.object(
                    document_cli,
                    "_connection_from_args",
                    side_effect=AssertionError("network/config path must not run"),
                ),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                exit_code = document_cli.main(arguments)

        self.assertEqual(exit_code, 0)
        self.assertEqual(errors.getvalue(), "")
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["network_action"])
        self.assertEqual(payload["operation"], "store-plan")

    def test_retrieve_cli_uses_all_selectors_and_returns_both_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = _key()
            transport = MemoryArchiveTransport()
            execute_store(
                build_store_plan(
                    key,
                    _write_pdf(root / "resume.pdf", b"resume"),
                    _write_pdf(root / "cover.pdf", b"cover"),
                ),
                transport,
            )
            destination = root / "retrieved"
            output = StringIO()
            errors = StringIO()
            arguments = [
                "retrieve",
                "--url",
                key.job_url,
                "--company",
                key.company,
                "--job-title",
                key.job_title,
                "--email",
                key.email_used,
                "--output-dir",
                str(destination),
            ]

            with (
                patch.object(
                    document_cli,
                    "_connection_from_args",
                    return_value=(None, transport),
                ),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                exit_code = document_cli.main(arguments)

            self.assertEqual(exit_code, 0, errors.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["archive_id"], key.archive_id)
            self.assertEqual(Path(payload["resume"]), destination / RESUME_STORED_NAME)
            self.assertEqual(
                Path(payload["cover_letter"]),
                destination / COVER_LETTER_STORED_NAME,
            )
            self.assertTrue((destination / RESUME_STORED_NAME).is_file())
            self.assertTrue((destination / COVER_LETTER_STORED_NAME).is_file())

    def test_generate_creates_a_matching_pair_and_archives_only_when_explicit(self) -> None:
        from job_application_automation.core import engine_shared
        from job_application_automation.resume import (
            career_narrative,
            cover_letter,
            generate,
            source,
        )

        class FakeCache:
            def load(self, path: Path) -> int:
                del path
                return 0

            def save(self, path: Path) -> None:
                del path

        captured: dict[str, object] = {}

        def fake_resume(job, output_path: Path, email_override: str):
            captured["resume_email"] = email_override
            captured["resume_url"] = job.url
            _write_pdf(output_path, b"generated resume")
            return output_path

        def fake_cover(job, narrative, resume_source, output_path: Path, **kwargs):
            del narrative, resume_source
            captured["cover_email"] = kwargs["email_override"]
            captured["cover_url"] = job.url
            _write_pdf(output_path, b"generated cover")
            output_path.with_name(output_path.stem + ".audit.json").write_text(
                "{}",
                encoding="utf-8",
            )
            return output_path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jd_path = root / "jd.txt"
            jd_path.write_text("Build trustworthy products.", encoding="utf-8")
            destination = root / "generated"
            transport = MemoryArchiveTransport()
            output = StringIO()
            errors = StringIO()
            arguments = [
                "generate",
                "--url",
                "https://jobs.example.com/roles/123?utm_source=test",
                "--company",
                "Example",
                "--job-title",
                "Product Manager",
                "--email",
                "Candidate@Example.com",
                "--jd-file",
                str(jd_path),
                "--output-dir",
                str(destination),
                "--archive",
            ]
            with (
                patch.object(engine_shared, "load_json_config", return_value={}),
                patch.object(career_narrative, "load_career_narrative", return_value=object()),
                patch.object(source, "load_resume_source", return_value=object()),
                patch.object(generate, "generate_personalized_resume", side_effect=fake_resume),
                patch.object(cover_letter, "generate_cover_letter", side_effect=fake_cover),
                patch.object(cover_letter, "CoverLetterCache", FakeCache),
                patch.object(
                    cover_letter,
                    "COVER_LETTER_CACHE_FILE",
                    root / "cover-cache.json",
                ),
                patch.object(
                    document_cli,
                    "_connection_from_args",
                    return_value=(None, transport),
                ),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                exit_code = document_cli.main(arguments)

            self.assertEqual(exit_code, 0, errors.getvalue())
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["archived"])
            self.assertEqual(payload["archive_status"], "STORED")
            self.assertTrue((destination / RESUME_STORED_NAME).is_file())
            self.assertTrue((destination / COVER_LETTER_STORED_NAME).is_file())
            self.assertTrue((destination / "cover_letter.audit.json").is_file())

        self.assertEqual(captured["resume_email"], "candidate@example.com")
        self.assertEqual(captured["cover_email"], "candidate@example.com")
        self.assertEqual(captured["resume_url"], "https://jobs.example.com/roles/123")
        self.assertEqual(captured["cover_url"], "https://jobs.example.com/roles/123")


if __name__ == "__main__":
    unittest.main()
