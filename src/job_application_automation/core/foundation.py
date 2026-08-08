"""Dependency-free exception taxonomy for workflow boundaries.

The compatibility bases are intentional.  Existing callers commonly catch
``ValueError``, ``OSError``, or ``RuntimeError`` at command boundaries; the
specialized errors preserve those contracts while allowing new code to make
precise recovery decisions.
"""

from __future__ import annotations

# Consolidated sections retain their local imports to keep each former module
# readable and mechanically comparable during the compatibility migration.
# ruff: noqa: E402


class JobAutomationError(Exception):
    """Base class for expected failures raised by this package."""


class ConfigurationError(JobAutomationError, ValueError):
    """A configuration document is missing, malformed, or internally invalid."""


class InputContractError(JobAutomationError, ValueError):
    """Caller-supplied data violates a public model or function contract."""


class ArtifactError(JobAutomationError, OSError, ValueError):
    """A persisted artifact cannot be read, validated, or written safely."""


class ExternalServiceError(JobAutomationError, RuntimeError):
    """A required subprocess, network API, or SDK operation failed."""


class BrowserAutomationError(ExternalServiceError):
    """A browser session or browser-control boundary could not complete."""


class ApplicationBlockedError(BrowserAutomationError):
    """The application cannot proceed without manual or external intervention."""


class SubmissionOutcomeUnknown(BrowserAutomationError):
    """A submit action may have occurred, but confirmation is inconclusive."""


"""Shared filesystem locations for the two-level project layout."""


import importlib.util
from pathlib import Path

# The implementation package lives below ``src`` and is launched through the
# single source-tree command runner. Keep exported paths anchored at the project
# layout so config, data, output, and subprocess references stay predictable.
PACKAGE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PACKAGE_DIR.parent
# Keep the single launcher usable in both supported execution modes. In a
# checkout it resolves to ``src/job_automation.py``; after installation the
# launcher is installed as a top-level module and its concrete location is
# discovered from import metadata instead of assuming a source-tree layout.
_CLI_MODULE_SPEC = importlib.util.find_spec("job_automation")
CLI_ENTRYPOINT = (
    Path(_CLI_MODULE_SPEC.origin)
    if _CLI_MODULE_SPEC is not None and _CLI_MODULE_SPEC.origin is not None
    else SRC_DIR / "job_automation.py"
)
PROJECT_ROOT = SRC_DIR.parent
if SRC_DIR.name != "src":
    # Wheels install the package below site-packages rather than a repository
    # ``src`` directory. Runtime data belongs to the caller's working tree in
    # that mode, never beside the installed package.
    PROJECT_ROOT = Path.cwd().resolve()
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"


def resolve_existing(path: str | Path, *search_dirs: Path) -> Path:
    """Resolve an absolute path or search named project directories in order."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    for directory in search_dirs:
        resolved = directory / candidate
        if resolved.exists():
            return resolved
    return (search_dirs[0] / candidate) if search_dirs else (PROJECT_ROOT / candidate)


def resolve_project_dir(path: str | Path, default: Path = OUTPUT_DIR) -> Path:
    """Resolve a configurable directory relative to the project root."""
    raw = str(path).strip()
    if not raw:
        return default
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


"""Shared normalization and validation for application identities."""


import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


_TRACKING_QUERY_KEYS = {
    "campaign",
    "fbclid",
    "gclid",
    "ref",
    "referrer",
    "source",
}
_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL_PART = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$")
_GREENHOUSE_JOB_PATH = re.compile(r"^/[^/]+/jobs/(?P<job_id>[^/]+)/?$", re.IGNORECASE)


def _greenhouse_path_job_id(host: str, path: str) -> str:
    """Return the job ID embedded in a provider-owned Greenhouse job path."""
    if host != "greenhouse.io" and not host.endswith(".greenhouse.io"):
        return ""
    match = _GREENHOUSE_JOB_PATH.fullmatch(path)
    return unquote(match.group("job_id")) if match is not None else ""


def _require_string(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InputContractError(f"{field_name} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise InputContractError(f"{field_name} cannot be empty")
    return normalized


def normalize_lookup_text(value: object, field_name: str) -> str:
    """Return a case-insensitive, whitespace-stable lookup value."""
    if not isinstance(value, str):
        raise InputContractError(f"{field_name} must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    if not normalized:
        raise InputContractError(f"{field_name} cannot be empty")
    return normalized


def normalize_email(value: object, field_name: str = "email") -> str:
    """Validate and normalize an email address for deterministic lookup.

    The local part is case-folded deliberately: candidate addresses in this
    project are account identifiers rather than case-sensitive SMTP routes.
    """
    if not isinstance(value, str):
        raise InputContractError(f"{field_name} must be a string")
    email = unicodedata.normalize("NFKC", value).strip()
    if (
        not email
        or len(email) > 254
        or any(character.isspace() for character in email)
        or email.count("@") != 1
    ):
        raise InputContractError(f"{field_name} must be a valid email address")
    local_part, domain = email.rsplit("@", 1)
    if (
        not local_part
        or len(local_part) > 64
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
        or not _LOCAL_PART.fullmatch(local_part)
    ):
        raise InputContractError(f"{field_name} must be a valid email address")
    try:
        ascii_domain = domain.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InputContractError(f"{field_name} must contain a valid domain") from exc
    labels = ascii_domain.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise InputContractError(f"{field_name} must contain a valid domain")
    return f"{local_part.casefold()}@{ascii_domain}"


def _uppercase_percent_escape(match: re.Match[str]) -> str:
    return match.group(0).upper()


def canonical_job_url(value: object) -> str:
    """Canonicalize an absolute HTTPS job URL without losing identity queries."""
    if not isinstance(value, str):
        raise InputContractError("job_url must be a string")
    raw_url = unicodedata.normalize("NFKC", value).strip()
    if not raw_url or any(character.isspace() or ord(character) < 32 for character in raw_url):
        raise InputContractError("job_url cannot be empty or contain whitespace/control characters")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise InputContractError("job_url must be a valid absolute HTTPS URL") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise InputContractError("job_url must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise InputContractError("job_url cannot contain credentials")

    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InputContractError("job_url must contain a valid hostname") from exc
    host_for_url = f"[{host}]" if ":" in host else host
    netloc = host_for_url if port in (None, 443) else f"{host_for_url}:{port}"

    path = re.sub(r"/+", "/", parsed.path or "/")
    path = re.sub(r"/(?:apply|application)/?$", "", path, flags=re.IGNORECASE)
    path = path.rstrip("/") or "/"
    path = _PERCENT_ESCAPE.sub(_uppercase_percent_escape, path)
    greenhouse_path_job_id = _greenhouse_path_job_id(host, path)

    retained_query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lookup_key = key.casefold()
        if lookup_key.startswith("utm_") or lookup_key in _TRACKING_QUERY_KEYS:
            continue
        if lookup_key == "gh_jid" and greenhouse_path_job_id and item == greenhouse_path_job_id:
            # Greenhouse sometimes appends the same ID already encoded by
            # ``/<board>/jobs/<id>``. Keep mismatched values because they can
            # identify a different embedded application.
            continue
        retained_query.append((key, item))
    retained_query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    query = urlencode(retained_query, doseq=True)
    return urlunsplit(("https", netloc, path, query, ""))


"""Lightweight validation and detection for supported ATS job URLs."""


import re
from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse


ATS_HOST_MARKERS: Mapping[str, tuple[str, ...]] = {
    "ashby": ("ashbyhq.com",),
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "workable": ("workable.com", "apply.workable.com"),
    "smartrecruiters": ("smartrecruiters.com", "jobs.smartrecruiters.com"),
}

# Requiring provider-owned job path shapes prevents a company board root from
# being mistaken for an individual application. Greenhouse embedded and custom
# domain forms are handled separately in ``validate_ats_job_url``.
ATS_JOB_PATH_PATTERNS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "ashby": (re.compile(r"^/[^/]+/[^/]+(?:/application)?/?$", re.I),),
    "greenhouse": (re.compile(r"^/[^/]+/jobs/[^/]+/?$", re.I),),
    "lever": (re.compile(r"^/[^/]+/[^/]+(?:/apply)?/?$", re.I),),
    "workable": (re.compile(r"^/(?:[^/]+/)?(?:j|jobs)/[^/]+(?:/(?:apply|application))?/?$", re.I),),
    "smartrecruiters": (
        re.compile(r"^/[^/]+/[^/]+/?$", re.I),
        re.compile(r"^/oneclick-ui/company/[^/]+/publication/[^/]+/?$", re.I),
    ),
}


def _host_matches(host: str, marker: str) -> bool:
    return host == marker or host.endswith(f".{marker}")


def validate_ats_url(url: str, ats: str) -> bool:
    """Return whether *url* belongs to the requested supported ATS."""
    if ats not in ATS_HOST_MARKERS:
        return False
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    greenhouse_job_id = parse_qs(parsed.query).get("gh_jid", [])
    # Some companies embed the Greenhouse form on their own career site. A
    # numeric gh_jid is the only provider-owned signal on those custom domains.
    custom_greenhouse_url = (
        ats == "greenhouse" and len(greenhouse_job_id) == 1 and greenhouse_job_id[0].isdigit()
    )
    return (
        parsed.scheme.lower() == "https"
        and bool(host)
        and (
            any(_host_matches(host, marker) for marker in ATS_HOST_MARKERS[ats])
            or custom_greenhouse_url
        )
    )


def validate_ats_job_url(url: str, ats: str) -> bool:
    """Return whether *url* identifies a job, not merely an ATS company board."""
    if not validate_ats_url(url, ats):
        return False
    patterns = ATS_JOB_PATH_PATTERNS.get(ats)
    if not patterns:
        return True
    try:
        parsed = urlparse(str(url).strip())
        path = parsed.path or "/"
    except ValueError:
        return False
    if ats == "greenhouse":
        query = parse_qs(parsed.query)
        gh_jid = query.get("gh_jid", [])
        embed_token = query.get("token", [])
        if len(gh_jid) == 1 and gh_jid[0].isdigit():
            return True
        if (
            path.rstrip("/").casefold() == "/embed/job_app"
            and len(embed_token) == 1
            and embed_token[0].isdigit()
        ):
            return True
    return any(pattern.fullmatch(path) for pattern in patterns)


def detect_ats_job_url(url: str) -> str | None:
    """Return the supported ATS that owns a job-specific URL."""
    if not isinstance(url, str) or not url.strip():
        return None
    return next(
        (name for name in ATS_HOST_MARKERS if validate_ats_job_url(url, name)),
        None,
    )


"""Atomic, dependency-free persistence helpers for workflow artifacts."""


import csv
import io
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterable, Iterator, Sequence


DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
DEFAULT_STALE_LOCK_SECONDS = 300.0


def _target_path(path: str | Path) -> Path:
    target = Path(path).expanduser()
    if not target.name:
        raise InputContractError("artifact path must name a file")
    return target


@contextmanager
def interprocess_file_lock(
    path: str | Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    stale_seconds: float = DEFAULT_STALE_LOCK_SECONDS,
) -> Iterator[None]:
    """Serialize read-modify-write updates made by independent processes."""
    if timeout_seconds <= 0:
        raise InputContractError("timeout_seconds must be greater than zero")
    if stale_seconds <= 0:
        raise InputContractError("stale_seconds must be greater than zero")

    target = _target_path(path)
    lock_path = target.with_name(f"{target.name}.lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            candidate = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(candidate, f"{os.getpid()}\n".encode("ascii"))
            except Exception:
                os.close(candidate)
                lock_path.unlink(missing_ok=True)
                raise
            descriptor = candidate
        except (FileExistsError, PermissionError):
            # Windows can report a sharing violation as PermissionError while
            # another process still owns (or is unlinking) the lock file.
            try:
                # Filesystem mtimes use wall-clock time; the deadline uses a
                # monotonic clock so clock adjustments cannot extend the wait.
                stale = time.time() - lock_path.stat().st_mtime > stale_seconds
            except FileNotFoundError:
                continue
            except PermissionError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except (FileNotFoundError, PermissionError):
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for artifact lock: {lock_path}") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """Write text by replacing the target only after the temporary file is ready."""
    if not isinstance(text, str):
        raise InputContractError("text must be a string")
    target = _target_path(path)
    temporary_name: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    except (OSError, UnicodeError) as exc:
        raise ArtifactError(f"could not write artifact {target}: {exc}") from exc
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
    return target


def read_json(path: str | Path) -> object:
    """Read one UTF-8 JSON artifact without adding application-specific defaults."""
    target = _target_path(path)
    try:
        with target.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"could not read JSON artifact {target}: {exc}") from exc


def write_json(
    path: str | Path,
    payload: object,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
) -> Path:
    """Atomically persist JSON while preserving the caller's selected format."""
    try:
        serialized = json.dumps(
            payload,
            indent=indent,
            ensure_ascii=ensure_ascii,
            sort_keys=sort_keys,
        )
    except (TypeError, ValueError) as exc:
        raise InputContractError("artifact payload must be JSON serializable") from exc
    return atomic_write_text(path, serialized)


def _normalized_fieldnames(
    rows: Sequence[Mapping[str, object]], fieldnames: Sequence[str] | None
) -> list[str]:
    if fieldnames is not None:
        normalized = list(fieldnames)
        if any(not isinstance(name, str) or not name for name in normalized):
            raise InputContractError("fieldnames must contain non-empty strings")
        if len(set(normalized)) != len(normalized):
            raise InputContractError("fieldnames cannot contain duplicates")
        return normalized
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if not isinstance(key, str) or not key:
                raise InputContractError("CSV row keys must be non-empty strings")
            if key not in seen:
                names.append(key)
                seen.add(key)
    return names


def write_csv(
    path: str | Path,
    rows: Iterable[Mapping[str, object]],
    *,
    fieldnames: Sequence[str] | None = None,
) -> Path:
    """Atomically write rows as a UTF-8 CSV with deterministic field ordering.

    With no explicit ``fieldnames``, columns follow first appearance across the
    supplied rows.  This keeps exported records stable without requiring each
    caller to duplicate schema bookkeeping.
    """
    materialized = list(rows)
    if not all(isinstance(row, Mapping) for row in materialized):
        raise InputContractError("rows must contain mappings")
    columns = _normalized_fieldnames(materialized, fieldnames)
    for row in materialized:
        unexpected = set(row).difference(columns)
        if unexpected:
            names = ", ".join(sorted(str(name) for name in unexpected))
            raise InputContractError(f"CSV row has fields outside fieldnames: {names}")
    buffer = io.StringIO(newline="")
    if columns:
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        writer.writerows(materialized)
    return atomic_write_text(path, buffer.getvalue())


"""Per-application screenshot isolation and cleanup."""


import shutil
from pathlib import Path


APPLICATION_SCREENSHOT_DIR_ENV = "JOB_APP_SCREENSHOT_DIR"
APPLICATION_SCREENSHOT_PARENT = ".application_screenshots"


def active_screenshot_directory(default: str | Path) -> Path:
    """Return the isolated directory selected by the application orchestrator."""
    override = os.environ.get(APPLICATION_SCREENSHOT_DIR_ENV, "").strip()
    return Path(override).expanduser() if override else Path(default).expanduser()


def _validated_child(path: str | Path, output_root: str | Path) -> tuple[Path, Path]:
    target = Path(path).expanduser().resolve()
    root = Path(output_root).expanduser().resolve()
    if target == root:
        raise ValueError("application screenshot directory cannot be the output root")
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"application screenshot directory must be inside the output root: {target}"
        ) from exc
    return target, root


def create_application_screenshot_directory(
    *,
    output_root: str | Path = OUTPUT_DIR,
    inherited: str | Path | None = None,
) -> Path:
    """Create or reuse one output-bound screenshot directory for an application."""
    if inherited is not None and str(inherited).strip():
        target, _ = _validated_child(inherited, output_root)
        target.mkdir(parents=True, exist_ok=True)
        return target

    root = Path(output_root).expanduser().resolve()
    parent = root / APPLICATION_SCREENSHOT_PARENT
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="application-", dir=parent))


def cleanup_application_screenshot_directory(
    directory: str | Path,
    *,
    output_root: str | Path = OUTPUT_DIR,
) -> tuple[int, int]:
    """Delete one isolated screenshot directory and return file/byte totals."""
    target, root = _validated_child(directory, output_root)
    if not target.exists():
        return 0, 0
    if not target.is_dir():
        raise ValueError(f"application screenshot path is not a directory: {target}")

    files_deleted = 0
    bytes_deleted = 0
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        files_deleted += 1
        try:
            bytes_deleted += path.stat().st_size
        except OSError:
            pass

    shutil.rmtree(target)
    parent = target.parent
    if parent != root:
        try:
            parent.rmdir()
        except OSError:
            pass
    return files_deleted, bytes_deleted


"""Typed, lossless runtime view of a normalized candidate profile.

ATS providers intentionally still receive dictionaries: that preserves the
configuration-v2 shape and existing provider patch points.  This value object
gives shared loaders a validated boundary without discarding provider-specific
or future configuration fields.
"""


from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


def _frozen_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"profile {field_name} must be an object")
    return MappingProxyType(dict(value))


def _optional_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    return _frozen_mapping(value, field_name)


@dataclass(frozen=True, slots=True)
class AutomationProfile:
    """Lossless normalized configuration used by ATS automation workflows."""

    candidate: Mapping[str, Any]
    rules: Mapping[str, Any]
    eeo_defaults: Mapping[str, Any]
    field_matchers: Mapping[str, Any]
    answer_variants: Mapping[str, Any]
    defaults: Mapping[str, Any]
    paths: Mapping[str, Any]
    company_overrides: Mapping[str, Any]
    document: Mapping[str, Any]

    @classmethod
    def from_runtime_mapping(cls, config: Mapping[str, Any]) -> AutomationProfile:
        """Freeze a normalized config while retaining unknown top-level fields."""
        if not isinstance(config, Mapping):
            raise ConfigurationError("profile config must be an object")
        document = dict(config)
        return cls(
            candidate=_frozen_mapping(document.get("candidate"), "candidate"),
            rules=_optional_mapping(document.get("rules"), "rules"),
            eeo_defaults=_optional_mapping(document.get("eeo_defaults"), "eeo_defaults"),
            field_matchers=_optional_mapping(document.get("field_matchers"), "field_matchers"),
            answer_variants=_optional_mapping(document.get("answer_variants"), "answer_variants"),
            defaults=_optional_mapping(document.get("defaults"), "defaults"),
            paths=_optional_mapping(document.get("paths"), "paths"),
            company_overrides=_optional_mapping(
                document.get("company_overrides"), "company_overrides"
            ),
            document=MappingProxyType(document),
        )

    def to_runtime_mapping(self) -> dict[str, Any]:
        """Return the legacy mutable dictionary shape expected by providers."""
        runtime = dict(self.document)
        runtime["candidate"] = dict(self.candidate)
        for name, value in (
            ("rules", self.rules),
            ("eeo_defaults", self.eeo_defaults),
            ("field_matchers", self.field_matchers),
            ("answer_variants", self.answer_variants),
            ("defaults", self.defaults),
            ("paths", self.paths),
            ("company_overrides", self.company_overrides),
        ):
            if name in self.document:
                runtime[name] = dict(value)
        return runtime
