"""CLI for generating, storing, and retrieving private application documents."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from collections.abc import Sequence

from .document_archive import (
    COVER_LETTER_STORED_NAME,
    DEFAULT_VPS_CONFIG,
    RESUME_STORED_NAME,
    ArchiveKey,
    ArchiveStorePlan,
    DocumentArchiveError,
    PuttyArchiveTransport,
    build_store_plan,
    execute_store,
    load_vps_archive_config,
    retrieve_archive,
)
from .foundation import CONFIG_DIR, OUTPUT_DIR

DEFAULT_PROFILE = CONFIG_DIR / "candidate_profile_config.json"
DEFAULT_GENERATED_ROOT = OUTPUT_DIR / "application_documents"
DEFAULT_RETRIEVED_ROOT = OUTPUT_DIR / "retrieved_documents"
COVER_LETTER_AUDIT_NAME = "cover_letter.audit.json"


def _print_payload(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", required=True, help="Absolute HTTPS job link")
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", "--job-title", dest="role", required=True)
    parser.add_argument("--email", required=True, help="Candidate email used for this document set")


def _add_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=str(DEFAULT_VPS_CONFIG),
        help="Ignored VPS credential/config JSON",
    )
    parser.add_argument(
        "--host-key",
        default="",
        help="Pinned PuTTY host-key fingerprint; overrides vps.ssh_host_key",
    )
    parser.add_argument(
        "--remote-root",
        default="",
        help="Private absolute VPS archive path; overrides vps.document_archive_root",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in seconds for each Plink/PSCP operation",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate and manage a private VPS archive of resumes/CVs and cover letters. "
            "The archive never uses Git or the public search-output branch."
        )
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    store = subparsers.add_parser(
        "store",
        help="Validate and optionally upload an existing CV and cover letter",
    )
    _add_identity_arguments(store)
    store.add_argument("--resume", "--cv", dest="resume", required=True)
    store.add_argument("--cover-letter", required=True)
    store.add_argument(
        "--execute",
        action="store_true",
        help="Perform the live VPS upload; without this flag only a local plan is shown",
    )
    _add_connection_arguments(store)

    retrieve = subparsers.add_parser(
        "retrieve",
        help="Retrieve both documents using URL, company, title, and email",
    )
    _add_identity_arguments(retrieve)
    retrieve.add_argument(
        "--output-dir",
        default="",
        help="Exact local destination directory (defaults to output/retrieved_documents/<id>)",
    )
    retrieve.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace only existing resume.pdf, cover_letter.pdf, and manifest.json",
    )
    _add_connection_arguments(retrieve)

    generate = subparsers.add_parser(
        "generate",
        help="Generate a matching CV and cover letter, with optional VPS archival",
    )
    _add_identity_arguments(generate)
    generate.add_argument("--keywords", default="")
    generate.add_argument("--jd-overview", default="")
    generate.add_argument("--jd-resp", default="")
    generate.add_argument("--jd-req", default="")
    generate.add_argument("--jd-file", default="", help="File containing the full job description")
    generate.add_argument("--location", default="")
    generate.add_argument("--profile", default=str(DEFAULT_PROFILE))
    generate.add_argument(
        "--output-dir",
        default="",
        help="Exact output directory (defaults to output/application_documents/<id>)",
    )
    generate.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace only this archive ID's generated document files",
    )
    generate.add_argument(
        "--archive",
        action="store_true",
        help="Upload both successfully generated PDFs to the private VPS archive",
    )
    _add_connection_arguments(generate)
    return parser


def _key_from_args(args: argparse.Namespace) -> ArchiveKey:
    return ArchiveKey(
        job_url=args.url,
        company=args.company,
        job_title=args.role,
        email_used=args.email,
    )


def _connection_from_args(args: argparse.Namespace):
    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    config = load_vps_archive_config(
        args.config,
        host_key_override=args.host_key,
        remote_root_override=args.remote_root,
    )
    return config, PuttyArchiveTransport(config, timeout_seconds=args.timeout)


def _plan_payload(plan: ArchiveStorePlan, *, executes_network: bool) -> dict[str, object]:
    return {
        "operation": "store" if executes_network else "store-plan",
        "network_action": executes_network,
        "archive_id": plan.archive_id,
        "relative_record_path": plan.relative_record_path,
        "record_fingerprint": plan.manifest.record_fingerprint,
        "documents": {
            "resume": {
                "sha256": plan.manifest.resume.sha256,
                "size_bytes": plan.manifest.resume.size_bytes,
            },
            "cover_letter": {
                "sha256": plan.manifest.cover_letter.sha256,
                "size_bytes": plan.manifest.cover_letter.size_bytes,
            },
        },
    }


def _store(args: argparse.Namespace) -> int:
    key = _key_from_args(args)
    plan = build_store_plan(key, args.resume, args.cover_letter)
    if not args.execute:
        _print_payload(_plan_payload(plan, executes_network=False))
        return 0
    _config, transport = _connection_from_args(args)
    result = execute_store(plan, transport)
    payload = _plan_payload(plan, executes_network=True)
    payload.update({"status": result.status, "remote_path": result.remote_path})
    _print_payload(payload)
    return 0


def _retrieve(args: argparse.Namespace) -> int:
    key = _key_from_args(args)
    destination = (
        Path(args.output_dir) if args.output_dir else DEFAULT_RETRIEVED_ROOT / key.archive_id
    )
    _config, transport = _connection_from_args(args)
    result = retrieve_archive(
        key,
        destination,
        transport,
        overwrite=args.overwrite,
    )
    _print_payload(
        {
            "operation": "retrieve",
            "archive_id": result.archive_id,
            "destination": str(result.destination),
            "resume": str(result.resume_path),
            "cover_letter": str(result.cover_letter_path),
            "manifest": str(result.manifest_path),
        }
    )
    return 0


def _load_job_description(args: argparse.Namespace) -> tuple[str, str, str]:
    overview = args.jd_overview
    responsibilities = args.jd_resp
    requirements = args.jd_req
    if args.jd_file:
        job_description_path = Path(args.jd_file).expanduser().resolve()
        if not job_description_path.is_file() or job_description_path.is_symlink():
            raise ValueError(f"--jd-file is not a regular file: {job_description_path}")
        overview = job_description_path.read_text(encoding="utf-8")
        responsibilities = ""
        requirements = ""
    combined = "\n".join(
        part for part in (overview, responsibilities, requirements) if part.strip()
    )
    if combined.strip():
        return overview, responsibilities, requirements

    # Keep module import lazy so every documents --help path is credential-free.
    from ..resume.ai_client import scrape_job

    try:
        scraped = scrape_job(args.url)
    except Exception as exc:
        raise ValueError(
            "No job-description text was supplied and the URL could not be loaded as an "
            "Ashby posting. Pass --jd-file or segmented --jd-* text."
        ) from exc
    overview = str(scraped.get("jd_text", ""))
    if not overview.strip():
        raise ValueError("The job URL did not provide usable job-description text")
    return overview, "", ""


def _promote_generated(stage: Path, destination: Path, *, overwrite: bool) -> None:
    supplied_destination = Path(destination).expanduser()
    if supplied_destination.is_symlink():
        raise ValueError("generated-document destination cannot be a symbolic link")
    target_directory = Path(os.path.abspath(supplied_destination))
    target_directory.mkdir(parents=True, exist_ok=True)
    if not target_directory.is_dir() or target_directory.is_symlink():
        raise ValueError("generated-document destination must be a real directory")

    names = (RESUME_STORED_NAME, COVER_LETTER_STORED_NAME, COVER_LETTER_AUDIT_NAME)
    targets = [target_directory / name for name in names]
    if any(target.is_symlink() for target in targets):
        raise ValueError("refusing to replace a symbolic-link document target")
    existing = [target for target in targets if target.exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "generated document files already exist; pass --overwrite to replace them"
        )

    token = secrets.token_hex(8)
    backups: dict[Path, Path] = {}
    promoted: list[Path] = []
    try:
        for target in existing:
            backup = target_directory / f".{target.name}.backup-{token}"
            os.replace(target, backup)
            backups[target] = backup
        for name, target in zip(names, targets, strict=True):
            source = stage / name
            if not source.is_file():
                raise OSError(f"generated artifact is missing: {source}")
            os.replace(source, target)
            promoted.append(target)
    except Exception as promotion_error:
        rollback_errors: list[str] = []
        for target in promoted:
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                rollback_errors.append(f"remove {target}: {exc}")
        for target, backup in backups.items():
            if backup.exists():
                try:
                    os.replace(backup, target)
                except OSError as exc:
                    rollback_errors.append(f"restore {backup}: {exc}")
        if rollback_errors:
            raise OSError(
                "generated-document promotion failed and rollback was incomplete; "
                "backups were preserved. " + "; ".join(rollback_errors)
            ) from promotion_error
        raise
    else:
        for backup in backups.values():
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass


def _generate(args: argparse.Namespace) -> int:
    key = _key_from_args(args)
    destination = (
        Path(args.output_dir) if args.output_dir else DEFAULT_GENERATED_ROOT / key.archive_id
    )
    destination = Path(os.path.abspath(destination.expanduser()))
    if destination.is_symlink():
        raise ValueError("generated-document destination cannot be a symbolic link")
    existing_targets = (
        destination / RESUME_STORED_NAME,
        destination / COVER_LETTER_STORED_NAME,
        destination / COVER_LETTER_AUDIT_NAME,
    )
    if any(path.exists() for path in existing_targets) and not args.overwrite:
        raise FileExistsError(
            "generated document files already exist; pass --overwrite to replace them"
        )

    overview, responsibilities, requirements = _load_job_description(args)

    # Heavy LLM/PDF imports stay below argument parsing to preserve lazy CLI help.
    from ..resume.career_narrative import load_career_narrative
    from ..resume.cover_letter import (
        COVER_LETTER_CACHE_FILE,
        CoverLetterCache,
        CoverLetterJob,
        generate_cover_letter,
    )
    from ..resume.generate import JobInfo, generate_personalized_resume
    from ..resume.source import load_resume_source
    from .engine_shared import load_json_config
    from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path

    try:
        profile = load_json_config(Path(args.profile))
        narrative = load_career_narrative(profile)
        source = load_resume_source(
            resolve_runtime_path(RUNTIME_CONFIG.application.resume_source_file)
        )
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not load generation source/config: {exc}") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{key.archive_id}.generate.",
            dir=destination.parent,
        )
    )
    try:
        resume_path = stage / RESUME_STORED_NAME
        cover_letter_path = stage / COVER_LETTER_STORED_NAME
        resume_job = JobInfo(
            company=key.company,
            role_title=key.job_title,
            keywords=args.keywords,
            jd_overview=overview,
            jd_responsibilities=responsibilities,
            jd_requirements=requirements,
            url=key.canonical_url,
            location=args.location,
        )
        if generate_personalized_resume(resume_job, resume_path, key.email_key) is None:
            raise DocumentArchiveError("Resume generation did not produce a valid PDF")

        cover_job = CoverLetterJob(
            company=key.company,
            role=key.job_title,
            jd_text="\n".join(
                part for part in (overview, responsibilities, requirements) if part.strip()
            ),
            url=key.canonical_url,
        )
        cache = CoverLetterCache()
        if COVER_LETTER_CACHE_FILE.exists():
            try:
                cache.load(COVER_LETTER_CACHE_FILE)
            except (OSError, ValueError):
                pass
        result = generate_cover_letter(
            cover_job,
            narrative,
            source,
            cover_letter_path,
            email_override=key.email_key,
            cache=cache,
        )
        if result is None:
            raise DocumentArchiveError(
                "Cover-letter generation did not produce a valid one-page PDF"
            )
        cache.save(COVER_LETTER_CACHE_FILE)
        _promote_generated(stage, destination, overwrite=args.overwrite)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    plan = build_store_plan(
        key,
        destination / RESUME_STORED_NAME,
        destination / COVER_LETTER_STORED_NAME,
    )
    payload: dict[str, object] = {
        "operation": "generate",
        "archive_id": key.archive_id,
        "destination": str(destination),
        "resume": str(destination / RESUME_STORED_NAME),
        "cover_letter": str(destination / COVER_LETTER_STORED_NAME),
        "cover_letter_audit": str(destination / COVER_LETTER_AUDIT_NAME),
        "archived": False,
    }
    if args.archive:
        _config, transport = _connection_from_args(args)
        try:
            stored = execute_store(plan, transport)
        except DocumentArchiveError as exc:
            raise DocumentArchiveError(
                f"VPS upload failed; generated documents remain at {destination}: {exc}"
            ) from exc
        payload.update(
            {
                "archived": True,
                "archive_status": stored.status,
                "remote_path": stored.remote_path,
            }
        )
    _print_payload(payload)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the private document archive CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.operation == "store":
            return _store(args)
        if args.operation == "retrieve":
            return _retrieve(args)
        if args.operation == "generate":
            return _generate(args)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"Invalid document archive input: {exc}", file=sys.stderr)
        return 2
    except (DocumentArchiveError, OSError) as exc:
        print(f"Document archive operation failed: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown document operation: {args.operation}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
