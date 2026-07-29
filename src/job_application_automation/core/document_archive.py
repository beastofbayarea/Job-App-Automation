"""Private, immutable VPS storage for generated application documents.

The archive is deliberately independent from Git and the public search-output
branch. Records are addressed by an opaque URL/email identity, contain a
strict manifest, and are verified before upload or local promotion.

==============================================================================
OUT-OF-THE-BOX ALTERNATE APPROACHES / ARCHITECTURAL OPTIONS:
1. Encrypted Content-Addressable Storage (CAS) on S3/R2/MinIO with Client-Side AES-256-GCM:
   - Instead of relying on raw SSH/rsync file transfers to a remote VPS directory structure,
     implement a cloud-native Content-Addressable Storage layer using S3 API compatible storage (AWS S3, Cloudflare R2, MinIO).
   - Documents are encrypted client-side with AES-256-GCM before upload, keyed strictly by SHA-256 content hashes.
   - Benefit: Provides zero-knowledge privacy, high availability across regions, instant HTTP CDN delivery, and eliminates SSH key management overhead.

2. Embedded Compressed SQLite / DuckDB Blob Vault with Columnar Search:
   - Package all archived resumes, cover letters, metadata manifests, and SHA-256 checksums into an embedded, ZSTD-compressed SQLite database file.
   - Benefit: Single-file portable database backing up thousands of applications, with SQL index querying capabilities over historical application text.

3. IPFS / Decentralized Filecoin Archive with Immutable CIDs:
   - Store generated application records immutably on IPFS (InterPlanetary File System) nodes using Content Identifiers (CIDs).
   - Benefit: Cryptographically verifiable document authenticity and tamper-proof historic record keeping.
==============================================================================
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Mapping, Protocol, Sequence

from .adapters import CommandResult, ProcessRunner, ProcessSettings
from .artifacts import write_json
from .identity import canonical_job_url, normalize_email, normalize_lookup_text
from .paths import PROJECT_ROOT


ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_IDENTITY_VERSION = 1
DEFAULT_ARCHIVE_ROOT = "/var/lib/job-application-automation/private-archive"
DEFAULT_VPS_CONFIG = PROJECT_ROOT / "config" / "vps_config.json"
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
RESUME_STORED_NAME = "resume.pdf"
COVER_LETTER_STORED_NAME = "cover_letter.pdf"
MANIFEST_STORED_NAME = "manifest.json"
FINGERPRINT_STORED_NAME = "record.sha256"

_ARCHIVE_ID_PATTERN = re.compile(r"^ja1_[0-9a-f]{64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REMOTE_ROOT = re.compile(r"^/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9.-]+$")
_SAFE_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class DocumentArchiveError(RuntimeError):
    """Raised when an archive transport or integrity operation fails."""


class ArchiveConflictError(DocumentArchiveError):
    """Raised when an immutable archive ID already has different content."""


class ArchiveNotFoundError(DocumentArchiveError):
    """Raised when no archive exists for a supplied identity."""


class ArchiveIntegrityError(DocumentArchiveError):
    """Raised when downloaded or remote content fails validation."""


def _display_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    return normalized


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _require_positive_size(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    if value > MAX_PDF_BYTES:
        raise ValueError(f"{field_name} exceeds the {MAX_PDF_BYTES}-byte archive limit")
    return value


def _require_aware_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return timestamp.isoformat()


def validate_remote_root(value: object) -> str:
    """Validate an absolute, shell-safe POSIX archive root outside the repo."""
    if not isinstance(value, str):
        raise ValueError("archive_root must be a string")
    root = value.strip().rstrip("/")
    if (
        not _SAFE_REMOTE_ROOT.fullmatch(root)
        or "/../" in f"{root}/"
        or root in {"/", "/root", "/home", "/var", "/var/lib"}
    ):
        raise ValueError(
            "archive_root must be a specific absolute POSIX path using only "
            "letters, numbers, '.', '_', and '-'"
        )
    lowered = root.casefold()
    if any(fragment in f"{lowered}/" for fragment in ("/www/", "/public_html/", "/htdocs/")):
        raise ValueError("archive_root cannot be inside a web-published directory")
    return root


@dataclass(frozen=True, slots=True)
class ArchiveKey:
    """The four user-facing selectors for one application-document record."""

    job_url: str
    company: str
    job_title: str
    email_used: str

    def __post_init__(self) -> None:
        if not isinstance(self.job_url, str) or not self.job_url.strip():
            raise ValueError("job_url cannot be empty")
        object.__setattr__(self, "job_url", self.job_url.strip())
        object.__setattr__(self, "company", _display_text(self.company, "company"))
        object.__setattr__(self, "job_title", _display_text(self.job_title, "job_title"))
        object.__setattr__(self, "email_used", _display_text(self.email_used, "email_used"))
        # Validate every derived key at construction time.
        canonical_job_url(self.job_url)
        normalize_lookup_text(self.company, "company")
        normalize_lookup_text(self.job_title, "job_title")
        normalize_email(self.email_used, "email_used")

    @property
    def canonical_url(self) -> str:
        return canonical_job_url(self.job_url)

    @property
    def company_key(self) -> str:
        return normalize_lookup_text(self.company, "company")

    @property
    def title_key(self) -> str:
        return normalize_lookup_text(self.job_title, "job_title")

    @property
    def email_key(self) -> str:
        return normalize_email(self.email_used, "email_used")

    @property
    def archive_id(self) -> str:
        identity = f"{self.canonical_url}\0{self.email_key}".encode("utf-8")
        return f"ja1_{hashlib.sha256(identity).hexdigest()}"

    def identity_payload(self) -> dict[str, str]:
        return {
            "job_url": self.job_url,
            "canonical_job_url": self.canonical_url,
            "company": self.company,
            "company_key": self.company_key,
            "job_title": self.job_title,
            "job_title_key": self.title_key,
            "email_used": self.email_used,
            "email_key": self.email_key,
        }


@dataclass(frozen=True, slots=True)
class ArchivedDocument:
    """Integrity metadata for one immutable PDF."""

    kind: str
    stored_name: str
    original_filename: str
    sha256: str
    size_bytes: int
    media_type: str = "application/pdf"

    def __post_init__(self) -> None:
        expected_name = {
            "resume": RESUME_STORED_NAME,
            "cover_letter": COVER_LETTER_STORED_NAME,
        }.get(self.kind)
        if expected_name is None:
            raise ValueError("document kind must be resume or cover_letter")
        if self.stored_name != expected_name:
            raise ValueError(f"{self.kind} stored_name must be {expected_name}")
        if (
            not isinstance(self.original_filename, str)
            or not self.original_filename
            or Path(self.original_filename).name != self.original_filename
            or len(self.original_filename) > 255
            or any(ord(character) < 32 for character in self.original_filename)
        ):
            raise ValueError("original_filename must be a basename")
        _require_sha256(self.sha256, f"{self.kind}.sha256")
        _require_positive_size(self.size_bytes, f"{self.kind}.size_bytes")
        if self.media_type != "application/pdf":
            raise ValueError("archived documents must use application/pdf")

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "stored_name": self.stored_name,
            "original_filename": self.original_filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }

    @classmethod
    def from_payload(cls, payload: object, expected_kind: str) -> "ArchivedDocument":
        if not isinstance(payload, Mapping):
            raise ValueError(f"{expected_kind} document metadata must be an object")
        if payload.get("kind") != expected_kind:
            raise ValueError(f"{expected_kind} document kind does not match its key")
        return cls(
            kind=expected_kind,
            stored_name=payload.get("stored_name"),  # type: ignore[arg-type]
            original_filename=payload.get("original_filename"),  # type: ignore[arg-type]
            sha256=payload.get("sha256"),  # type: ignore[arg-type]
            size_bytes=payload.get("size_bytes"),  # type: ignore[arg-type]
            media_type=payload.get("media_type"),  # type: ignore[arg-type]
        )


def _record_fingerprint(
    key: ArchiveKey,
    resume: ArchivedDocument,
    cover_letter: ArchivedDocument,
) -> str:
    payload = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "identity_version": ARCHIVE_IDENTITY_VERSION,
        "archive_id": key.archive_id,
        "canonical_job_url": key.canonical_url,
        "company_key": key.company_key,
        "job_title_key": key.title_key,
        "email_key": key.email_key,
        "documents": {
            "resume": {
                "sha256": resume.sha256,
                "size_bytes": resume.size_bytes,
            },
            "cover_letter": {
                "sha256": cover_letter.sha256,
                "size_bytes": cover_letter.size_bytes,
            },
        },
    }
    return _sha256_bytes(_stable_json(payload))


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    """Strict manifest stored beside one resume and cover letter."""

    key: ArchiveKey
    resume: ArchivedDocument
    cover_letter: ArchivedDocument
    created_at: str
    archive_id: str = field(init=False)
    record_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "created_at", _require_aware_timestamp(self.created_at, "created_at")
        )
        object.__setattr__(self, "archive_id", self.key.archive_id)
        object.__setattr__(
            self,
            "record_fingerprint",
            _record_fingerprint(self.key, self.resume, self.cover_letter),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "identity_version": ARCHIVE_IDENTITY_VERSION,
            "archive_id": self.archive_id,
            "record_fingerprint": self.record_fingerprint,
            "created_at": self.created_at,
            "identity": self.key.identity_payload(),
            "documents": {
                "resume": self.resume.to_payload(),
                "cover_letter": self.cover_letter.to_payload(),
            },
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ArchiveManifest":
        if not isinstance(payload, Mapping):
            raise ValueError("archive manifest root must be an object")
        if payload.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
            raise ValueError(f"archive manifest schema_version must be {ARCHIVE_SCHEMA_VERSION}")
        if payload.get("identity_version") != ARCHIVE_IDENTITY_VERSION:
            raise ValueError(
                f"archive manifest identity_version must be {ARCHIVE_IDENTITY_VERSION}"
            )
        identity = payload.get("identity")
        documents = payload.get("documents")
        if not isinstance(identity, Mapping) or not isinstance(documents, Mapping):
            raise ValueError("archive manifest identity and documents must be objects")
        key = ArchiveKey(
            job_url=identity.get("job_url"),  # type: ignore[arg-type]
            company=identity.get("company"),  # type: ignore[arg-type]
            job_title=identity.get("job_title"),  # type: ignore[arg-type]
            email_used=identity.get("email_used"),  # type: ignore[arg-type]
        )
        if identity.get("canonical_job_url") != key.canonical_url:
            raise ValueError("archive manifest canonical_job_url is invalid")
        if identity.get("company_key") != key.company_key:
            raise ValueError("archive manifest company_key is invalid")
        if identity.get("job_title_key") != key.title_key:
            raise ValueError("archive manifest job_title_key is invalid")
        if identity.get("email_key") != key.email_key:
            raise ValueError("archive manifest email_key is invalid")
        manifest = cls(
            key=key,
            resume=ArchivedDocument.from_payload(documents.get("resume"), "resume"),
            cover_letter=ArchivedDocument.from_payload(
                documents.get("cover_letter"), "cover_letter"
            ),
            created_at=payload.get("created_at"),  # type: ignore[arg-type]
        )
        if payload.get("archive_id") != manifest.archive_id:
            raise ValueError("archive manifest archive_id does not match its identity")
        if payload.get("record_fingerprint") != manifest.record_fingerprint:
            raise ValueError("archive manifest record_fingerprint is invalid")
        return manifest

    def require_matching_key(self, supplied: ArchiveKey) -> None:
        """Require every supplied selector to match before exposing documents."""
        if supplied.archive_id != self.archive_id:
            raise ArchiveNotFoundError("No archive matches the supplied job URL and email")
        mismatches = []
        if supplied.canonical_url != self.key.canonical_url:
            mismatches.append("job URL")
        if supplied.company_key != self.key.company_key:
            mismatches.append("company")
        if supplied.title_key != self.key.title_key:
            mismatches.append("job title")
        if supplied.email_key != self.key.email_key:
            mismatches.append("email")
        if mismatches:
            raise ArchiveNotFoundError(
                "Archive identity exists, but these selectors do not match: "
                + ", ".join(mismatches)
            )


@dataclass(frozen=True, slots=True)
class ArchiveStorePlan:
    """Validated local inputs and immutable metadata for one upload."""

    manifest: ArchiveManifest
    resume_path: Path
    cover_letter_path: Path

    @property
    def archive_id(self) -> str:
        return self.manifest.archive_id

    @property
    def relative_record_path(self) -> str:
        return relative_record_path(self.archive_id)


@dataclass(frozen=True, slots=True)
class ArchiveStoreResult:
    archive_id: str
    status: str
    remote_path: str
    record_fingerprint: str


@dataclass(frozen=True, slots=True)
class ArchiveRetrieveResult:
    archive_id: str
    destination: Path
    resume_path: Path
    cover_letter_path: Path
    manifest_path: Path


def _validated_pdf(path: str | Path, kind: str, stored_name: str) -> tuple[Path, ArchivedDocument]:
    supplied = Path(path).expanduser()
    if supplied.is_symlink():
        raise ValueError(f"{kind} PDF cannot be a symbolic link")
    source = supplied.resolve()
    if not source.is_file():
        raise ValueError(f"{kind} PDF does not exist: {source}")
    size = source.stat().st_size
    _require_positive_size(size, f"{kind}.size_bytes")
    with source.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError(f"{kind} must be a PDF file")
    return source, ArchivedDocument(
        kind=kind,
        stored_name=stored_name,
        original_filename=source.name,
        sha256=_sha256_file(source),
        size_bytes=size,
    )


def build_store_plan(
    key: ArchiveKey,
    resume_path: str | Path,
    cover_letter_path: str | Path,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ArchiveStorePlan:
    """Validate two PDFs and construct their deterministic immutable manifest."""
    resume_source, resume = _validated_pdf(resume_path, "resume", RESUME_STORED_NAME)
    cover_source, cover = _validated_pdf(
        cover_letter_path,
        "cover_letter",
        COVER_LETTER_STORED_NAME,
    )
    active_clock = clock or (lambda: datetime.now(timezone.utc))
    created_at = active_clock()
    if not isinstance(created_at, datetime):
        raise ValueError("archive clock must return a datetime")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("archive clock must return a timezone-aware datetime")
    manifest = ArchiveManifest(
        key=key,
        resume=resume,
        cover_letter=cover,
        created_at=created_at.isoformat(),
    )
    if len(json.dumps(manifest.to_payload(), indent=2, ensure_ascii=False).encode("utf-8")) > (
        MAX_MANIFEST_BYTES
    ):
        raise ValueError("archive manifest exceeds the maximum supported size")
    return ArchiveStorePlan(
        manifest=manifest,
        resume_path=resume_source,
        cover_letter_path=cover_source,
    )


def relative_record_path(archive_id: str) -> str:
    if not _ARCHIVE_ID_PATTERN.fullmatch(archive_id):
        raise ValueError("archive_id is invalid")
    digest = archive_id.removeprefix("ja1_")
    return f"records/{digest[:2]}/{archive_id}"


@dataclass(frozen=True, slots=True)
class VpsArchiveConfig:
    """Validated PuTTY connection settings for the private archive."""

    host: str
    ssh_user: str
    host_key: str
    archive_root: str = DEFAULT_ARCHIVE_ROOT
    ssh_port: int = 22
    private_key_file: Path | None = None
    password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not _SAFE_HOST.fullmatch(self.host.strip()):
            raise ValueError("vps.host must be a DNS name or IPv4 address")
        if not isinstance(self.ssh_user, str) or not _SAFE_USER.fullmatch(self.ssh_user.strip()):
            raise ValueError("vps.ssh_user is invalid")
        if (
            not isinstance(self.host_key, str)
            or not self.host_key.strip()
            or "<" in self.host_key
            or "replace" in self.host_key.casefold()
            or any(character in "\r\n" for character in self.host_key)
        ):
            raise ValueError("vps.ssh_host_key is required for pinned archive connections")
        if (
            isinstance(self.ssh_port, bool)
            or not isinstance(self.ssh_port, int)
            or not 1 <= self.ssh_port <= 65535
        ):
            raise ValueError("vps.ssh_port must be between 1 and 65535")
        object.__setattr__(self, "host", self.host.strip())
        object.__setattr__(self, "ssh_user", self.ssh_user.strip())
        object.__setattr__(self, "host_key", self.host_key.strip())
        object.__setattr__(self, "archive_root", validate_remote_root(self.archive_root))

        key_file = self.private_key_file
        if key_file is not None:
            resolved_key = Path(key_file).expanduser().resolve()
            if not resolved_key.is_file():
                raise ValueError(f"archive private key file not found: {resolved_key}")
            object.__setattr__(self, "private_key_file", resolved_key)
            object.__setattr__(self, "password", None)
        elif not isinstance(self.password, str) or not self.password:
            raise ValueError("configure vps.archive_private_key_file or vps.ssh_password.value")
        elif "replace" in self.password.casefold():
            raise ValueError("vps.ssh_password.value still contains a placeholder")
        elif any(character in "\r\n" for character in self.password):
            raise ValueError("vps.ssh_password.value cannot contain newlines")

    @property
    def target(self) -> str:
        return f"{self.ssh_user}@{self.host}"

    def absolute_record_path(self, archive_id: str) -> str:
        return str(PurePosixPath(self.archive_root) / relative_record_path(archive_id))


def _optional_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def load_vps_archive_config(
    path: str | Path = DEFAULT_VPS_CONFIG,
    *,
    host_key_override: str = "",
    remote_root_override: str = "",
) -> VpsArchiveConfig:
    """Load private VPS settings without exposing credentials in diagnostics."""
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            document = json.load(stream)
    except FileNotFoundError as exc:
        raise ValueError(f"VPS config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"VPS config is not valid JSON: {config_path}") from exc
    if not isinstance(document, Mapping) or not isinstance(document.get("vps"), Mapping):
        raise ValueError("VPS config must contain a vps object")
    vps = document["vps"]
    assert isinstance(vps, Mapping)

    private_key_value = _optional_text(vps, "archive_private_key_file")
    private_key: Path | None = None
    if private_key_value:
        candidate = Path(private_key_value).expanduser()
        private_key = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate

    password: str | None = None
    password_object = vps.get("ssh_password")
    if isinstance(password_object, Mapping):
        password_value = password_object.get("value")
        password = password_value if isinstance(password_value, str) else None

    port_value = vps.get("ssh_port", 22)
    return VpsArchiveConfig(
        host=_optional_text(vps, "host"),
        ssh_user=_optional_text(vps, "ssh_user"),
        host_key=host_key_override.strip() or _optional_text(vps, "ssh_host_key"),
        archive_root=remote_root_override.strip()
        or _optional_text(vps, "document_archive_root")
        or DEFAULT_ARCHIVE_ROOT,
        ssh_port=port_value,  # type: ignore[arg-type]
        private_key_file=private_key,
        password=password,
    )


class ArchiveTransport(Protocol):
    """Binary record transport used by the archive service."""

    def remote_record_path(self, archive_id: str) -> str:
        """Return the private absolute remote path for an archive ID."""

    def store_record(self, plan: ArchiveStorePlan) -> str:
        """Store one immutable record and return STORED or ALREADY_STORED."""

    def download_record(self, archive_id: str, destination: Path) -> None:
        """Download one complete record into an existing temporary directory."""


class SubprocessCommandRunner:
    """Production subprocess adapter for PuTTY commands."""

    def run(self, command: Sequence[str], settings: ProcessSettings) -> CommandResult:
        completed = subprocess.run(
            list(command),
            cwd=settings.cwd,
            env=dict(settings.environment) if settings.environment else None,
            text=True,
            capture_output=True,
            timeout=settings.timeout_seconds,
            check=False,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class PuttyArchiveTransport:
    """Pinned-host-key Plink/PSCP transport with password-file hygiene."""

    def __init__(
        self,
        config: VpsArchiveConfig,
        *,
        process_runner: ProcessRunner | None = None,
        plink_path: str | None = None,
        pscp_path: str | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self.config = config
        self._runner = process_runner or SubprocessCommandRunner()
        self._plink = plink_path or shutil.which("plink") or ""
        self._pscp = pscp_path or shutil.which("pscp") or ""
        if not self._plink:
            raise ValueError("plink.exe was not found on PATH")
        if not self._pscp:
            raise ValueError("pscp.exe was not found on PATH")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive integer")
        self._settings = ProcessSettings(timeout_seconds=timeout_seconds)

    def remote_record_path(self, archive_id: str) -> str:
        return self.config.absolute_record_path(archive_id)

    @contextmanager
    def _credential_arguments(self) -> Iterator[list[str]]:
        if self.config.private_key_file is not None:
            yield ["-i", str(self.config.private_key_file)]
            return

        password_file: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix="job-archive-putty-", suffix=".txt")
            password_file = Path(name)
            try:
                os.chmod(password_file, 0o600)
            except Exception:
                os.close(descriptor)
                raise
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(self.config.password or "")
            yield ["-pwfile", str(password_file)]
        finally:
            if password_file is not None:
                password_file.unlink(missing_ok=True)

    def _plink_command(self, credentials: Sequence[str], remote_command: str) -> list[str]:
        return [
            self._plink,
            "-ssh",
            "-batch",
            "-P",
            str(self.config.ssh_port),
            "-hostkey",
            self.config.host_key,
            *credentials,
            self.config.target,
            remote_command,
        ]

    def _pscp_command(
        self,
        credentials: Sequence[str],
        source: str,
        destination: str,
    ) -> list[str]:
        return [
            self._pscp,
            "-batch",
            "-P",
            str(self.config.ssh_port),
            "-hostkey",
            self.config.host_key,
            *credentials,
            source,
            destination,
        ]

    def _run_checked(self, command: Sequence[str], operation: str) -> str:
        try:
            result = self._runner.run(command, self._settings)
        except subprocess.TimeoutExpired as exc:
            raise DocumentArchiveError(
                f"{operation} timed out after {self._settings.timeout_seconds} seconds"
            ) from exc
        except OSError as exc:
            raise DocumentArchiveError(f"{operation} could not start: {exc}") from exc
        if result.returncode != 0:
            diagnostics = (result.stderr or result.stdout).strip()[-2000:]
            suffix = f": {diagnostics}" if diagnostics else ""
            raise DocumentArchiveError(
                f"{operation} failed with exit code {result.returncode}{suffix}"
            )
        return result.stdout.strip()

    def _prepare_command(self, incoming: str, record_parent: str) -> str:
        root = shlex.quote(self.config.archive_root)
        incoming_value = shlex.quote(incoming)
        parent = shlex.quote(record_parent)
        statements = [
            "set -eu",
            "umask 077",
            f"test ! -L {root}",
            f"mkdir -p -- {root}",
            f"chmod 700 -- {root}",
            f"test ! -L {root}/.incoming",
            f"test ! -L {root}/records",
            f"test ! -L {parent}",
            f"mkdir -p -- {root}/.incoming {root}/records {parent}",
            f"test ! -L {root}/.incoming",
            f"test ! -L {root}/records",
            f"test ! -L {parent}",
            f"test ! -e {incoming_value}",
            f"mkdir -- {incoming_value}",
            f"chmod 700 -- {incoming_value}",
        ]
        return "; ".join(statements)

    @staticmethod
    def _file_check(path: str, digest: str, size: int) -> list[str]:
        quoted_path = shlex.quote(path)
        quoted_digest = shlex.quote(digest)
        return [
            f"test -f {quoted_path}",
            f"test ! -L {quoted_path}",
            f'test "$(wc -c < {quoted_path})" -eq {size}',
            f'actual="$(sha256sum -- {quoted_path})"',
            'actual="${actual%% *}"',
            f'test "$actual" = {quoted_digest}',
        ]

    def _commit_command(
        self,
        incoming: str,
        final: str,
        plan: ArchiveStorePlan,
        manifest_sha256: str,
        manifest_size: int,
    ) -> str:
        incoming_files = {
            RESUME_STORED_NAME: plan.manifest.resume,
            COVER_LETTER_STORED_NAME: plan.manifest.cover_letter,
        }
        statements = ["set -eu", "umask 077"]
        for name, document in incoming_files.items():
            statements.extend(
                self._file_check(
                    str(PurePosixPath(incoming) / name),
                    document.sha256,
                    document.size_bytes,
                )
            )
        statements.extend(
            self._file_check(
                str(PurePosixPath(incoming) / MANIFEST_STORED_NAME),
                manifest_sha256,
                manifest_size,
            )
        )
        fingerprint_path = str(PurePosixPath(incoming) / FINGERPRINT_STORED_NAME)
        statements.extend(
            [
                f"test -f {shlex.quote(fingerprint_path)}",
                f"test ! -L {shlex.quote(fingerprint_path)}",
                (
                    f"test \"$(tr -d '\\r\\n' < {shlex.quote(fingerprint_path)})\" = "
                    f"{shlex.quote(plan.manifest.record_fingerprint)}"
                ),
            ]
        )

        final_fingerprint = str(PurePosixPath(final) / FINGERPRINT_STORED_NAME)
        existing_checks = [
            f"test -d {shlex.quote(final)}",
            f"test ! -L {shlex.quote(final)}",
            f"test -f {shlex.quote(final_fingerprint)}",
            (
                f"test \"$(tr -d '\\r\\n' < {shlex.quote(final_fingerprint)})\" = "
                f"{shlex.quote(plan.manifest.record_fingerprint)}"
            ),
        ]
        for name, document in incoming_files.items():
            existing_checks.extend(
                self._file_check(
                    str(PurePosixPath(final) / name),
                    document.sha256,
                    document.size_bytes,
                )
            )
        existing_condition = " && ".join(existing_checks)
        existing_block = (
            f"if {existing_condition}; then "
            f"rm -rf -- {shlex.quote(incoming)}; printf ALREADY_STORED; exit 0; "
            "else printf ARCHIVE_CONFLICT >&2; exit 73; fi"
        )
        statements.extend(
            [
                f"chmod 600 -- {shlex.quote(incoming)}/*",
                f"if test -e {shlex.quote(final)}; then {existing_block}; fi",
                f"mv -T -- {shlex.quote(incoming)} {shlex.quote(final)}",
                f"chmod 700 -- {shlex.quote(final)}",
                "printf STORED",
            ]
        )
        return "; ".join(statements)

    def _cleanup_command(self, incoming: str) -> str:
        return "; ".join(
            [
                "set -eu",
                f"test ! -L {shlex.quote(incoming)}",
                f"rm -rf -- {shlex.quote(incoming)}",
            ]
        )

    def store_record(self, plan: ArchiveStorePlan) -> str:
        token = secrets.token_hex(16)
        incoming = str(PurePosixPath(self.config.archive_root) / ".incoming" / token)
        final = self.remote_record_path(plan.archive_id)
        record_parent = str(PurePosixPath(final).parent)

        with tempfile.TemporaryDirectory(prefix="job-document-archive-") as directory:
            stage = Path(directory)
            local_files = {
                RESUME_STORED_NAME: stage / RESUME_STORED_NAME,
                COVER_LETTER_STORED_NAME: stage / COVER_LETTER_STORED_NAME,
                MANIFEST_STORED_NAME: stage / MANIFEST_STORED_NAME,
                FINGERPRINT_STORED_NAME: stage / FINGERPRINT_STORED_NAME,
            }
            shutil.copyfile(plan.resume_path, local_files[RESUME_STORED_NAME])
            shutil.copyfile(plan.cover_letter_path, local_files[COVER_LETTER_STORED_NAME])
            write_json(
                local_files[MANIFEST_STORED_NAME],
                plan.manifest.to_payload(),
                indent=2,
                sort_keys=True,
            )
            local_files[FINGERPRINT_STORED_NAME].write_text(
                plan.manifest.record_fingerprint + "\n",
                encoding="ascii",
            )
            manifest_sha256 = _sha256_file(local_files[MANIFEST_STORED_NAME])
            manifest_size = local_files[MANIFEST_STORED_NAME].stat().st_size

            with self._credential_arguments() as credentials:
                prepared = False
                try:
                    self._run_checked(
                        self._plink_command(
                            credentials,
                            self._prepare_command(incoming, record_parent),
                        ),
                        "VPS archive staging",
                    )
                    prepared = True
                    for stored_name, local_path in local_files.items():
                        remote_target = (
                            f"{self.config.target}:{PurePosixPath(incoming) / stored_name}"
                        )
                        self._run_checked(
                            self._pscp_command(credentials, str(local_path), remote_target),
                            f"uploading {stored_name}",
                        )
                    status = self._run_checked(
                        self._plink_command(
                            credentials,
                            self._commit_command(
                                incoming,
                                final,
                                plan,
                                manifest_sha256,
                                manifest_size,
                            ),
                        ),
                        "committing VPS archive record",
                    )
                except DocumentArchiveError as exc:
                    if prepared:
                        try:
                            self._run_checked(
                                self._plink_command(
                                    credentials,
                                    self._cleanup_command(incoming),
                                ),
                                "cleaning failed VPS archive staging",
                            )
                        except DocumentArchiveError:
                            pass
                    if "ARCHIVE_CONFLICT" in str(exc):
                        raise ArchiveConflictError(
                            "An archive already exists for this job URL and email "
                            "with different metadata or documents"
                        ) from exc
                    raise

        terminal_status = status.splitlines()[-1].strip() if status else ""
        if terminal_status not in {"STORED", "ALREADY_STORED"}:
            raise DocumentArchiveError("VPS archive commit returned an unexpected status")
        return terminal_status

    def _verify_download_command(self, final: str) -> str:
        checks = [
            f"test -d {shlex.quote(final)}",
            f"test ! -L {shlex.quote(final)}",
        ]
        for name in (
            MANIFEST_STORED_NAME,
            RESUME_STORED_NAME,
            COVER_LETTER_STORED_NAME,
        ):
            path = str(PurePosixPath(final) / name)
            checks.extend([f"test -f {shlex.quote(path)}", f"test ! -L {shlex.quote(path)}"])
        condition = " && ".join(checks)
        return (
            "set -eu; "
            f"if {condition}; then printf ARCHIVE_FOUND; "
            "else printf ARCHIVE_NOT_FOUND >&2; exit 44; fi"
        )

    def download_record(self, archive_id: str, destination: Path) -> None:
        final = self.remote_record_path(archive_id)
        destination.mkdir(parents=True, exist_ok=True)
        with self._credential_arguments() as credentials:
            try:
                self._run_checked(
                    self._plink_command(credentials, self._verify_download_command(final)),
                    "locating VPS archive record",
                )
            except DocumentArchiveError as exc:
                if "ARCHIVE_NOT_FOUND" in str(exc):
                    raise ArchiveNotFoundError(
                        "No complete VPS archive record matches the supplied URL and email"
                    ) from exc
                raise
            for stored_name in (
                MANIFEST_STORED_NAME,
                RESUME_STORED_NAME,
                COVER_LETTER_STORED_NAME,
            ):
                remote_source = f"{self.config.target}:{PurePosixPath(final) / stored_name}"
                self._run_checked(
                    self._pscp_command(
                        credentials,
                        remote_source,
                        str(destination / stored_name),
                    ),
                    f"downloading {stored_name}",
                )


def _read_manifest(path: Path) -> ArchiveManifest:
    size = path.stat().st_size
    if size <= 0 or size > MAX_MANIFEST_BYTES:
        raise ArchiveIntegrityError("Downloaded archive manifest has an invalid size")
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        return ArchiveManifest.from_payload(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ArchiveIntegrityError(f"Downloaded archive manifest is invalid: {exc}") from exc


def _verify_downloaded_document(path: Path, document: ArchivedDocument) -> None:
    if not path.is_file() or path.is_symlink():
        raise ArchiveIntegrityError(f"Downloaded {document.kind} is not a regular file")
    if path.stat().st_size != document.size_bytes:
        raise ArchiveIntegrityError(f"Downloaded {document.kind} size does not match its manifest")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ArchiveIntegrityError(f"Downloaded {document.kind} is not a PDF")
    if _sha256_file(path) != document.sha256:
        raise ArchiveIntegrityError(
            f"Downloaded {document.kind} checksum does not match its manifest"
        )


def execute_store(
    plan: ArchiveStorePlan,
    transport: ArchiveTransport,
) -> ArchiveStoreResult:
    """Upload a validated plan through an explicit transport."""
    status = transport.store_record(plan)
    if status not in {"STORED", "ALREADY_STORED"}:
        raise DocumentArchiveError("Archive transport returned an invalid store status")
    return ArchiveStoreResult(
        archive_id=plan.archive_id,
        status=status,
        remote_path=transport.remote_record_path(plan.archive_id),
        record_fingerprint=plan.manifest.record_fingerprint,
    )


def _promote_download(
    stage: Path,
    destination: Path,
    *,
    overwrite: bool,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("destination must be a real directory")
    names = (RESUME_STORED_NAME, COVER_LETTER_STORED_NAME, MANIFEST_STORED_NAME)
    targets = [destination / name for name in names]
    for target in targets:
        if target.is_symlink():
            raise ValueError(f"refusing to replace symbolic link: {target}")
    existing = [target for target in targets if target.exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "destination already contains archive files; pass --overwrite to replace them"
        )

    token = secrets.token_hex(8)
    backups: dict[Path, Path] = {}
    promoted: list[Path] = []
    try:
        for target in existing:
            backup = destination / f".{target.name}.backup-{token}"
            os.replace(target, backup)
            backups[target] = backup
        for name, target in zip(names, targets):
            os.replace(stage / name, target)
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
                "archive promotion failed and rollback was incomplete; backups were "
                "preserved. " + "; ".join(rollback_errors)
            ) from promotion_error
        raise
    else:
        for backup in backups.values():
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass


def retrieve_archive(
    key: ArchiveKey,
    destination: str | Path,
    transport: ArchiveTransport,
    *,
    overwrite: bool = False,
) -> ArchiveRetrieveResult:
    """Retrieve both PDFs, verify identity and hashes, then promote locally."""
    supplied_destination = Path(destination).expanduser()
    if supplied_destination.is_symlink():
        raise ValueError("destination must not be a symbolic link")
    output_directory = Path(os.path.abspath(supplied_destination))
    if output_directory.exists():
        if output_directory.is_symlink() or not output_directory.is_dir():
            raise ValueError("destination must be a real directory")
        archive_targets = (
            output_directory / RESUME_STORED_NAME,
            output_directory / COVER_LETTER_STORED_NAME,
            output_directory / MANIFEST_STORED_NAME,
        )
        if any(path.exists() for path in archive_targets) and not overwrite:
            raise FileExistsError(
                "destination already contains archive files; pass --overwrite to replace them"
            )
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{key.archive_id}.",
            dir=output_directory.parent,
        )
    )
    try:
        transport.download_record(key.archive_id, stage)
        manifest = _read_manifest(stage / MANIFEST_STORED_NAME)
        manifest.require_matching_key(key)
        _verify_downloaded_document(stage / RESUME_STORED_NAME, manifest.resume)
        _verify_downloaded_document(
            stage / COVER_LETTER_STORED_NAME,
            manifest.cover_letter,
        )
        _promote_download(stage, output_directory, overwrite=overwrite)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return ArchiveRetrieveResult(
        archive_id=key.archive_id,
        destination=output_directory,
        resume_path=output_directory / RESUME_STORED_NAME,
        cover_letter_path=output_directory / COVER_LETTER_STORED_NAME,
        manifest_path=output_directory / MANIFEST_STORED_NAME,
    )
