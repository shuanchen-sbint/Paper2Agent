#!/usr/bin/env python3
"""Named external artifacts (model checkpoints, reference data) for a generated MCP server.

Copy this file unchanged next to the server entry point (``src/mcp_artifacts.py``)
together with an ``artifacts.json`` manifest, as required by
references/external-artifacts.md. Standard library only.

Tools resolve artifacts by *name*; the location comes from the deployment:

    $MCP_ARTIFACTS_DIR/<server>/<name>/   when MCP_ARTIFACTS_DIR is set
    <this directory>/artifacts/<name>/    otherwise

In a tool::

    from mcp_artifacts import resolve
    model_dir = resolve(model)          # clear error naming the fetch command if missing

Operators and deployers::

    python mcp_artifacts.py list                      # JSON: every artifact, installed or not
    python mcp_artifacts.py fetch --default           # install the default artifacts
    python mcp_artifacts.py fetch pistachio           # install named artifacts
    python mcp_artifacts.py adopt pistachio --from DIR  # reuse an already extracted copy
    python mcp_artifacts.py check                     # validate the manifest

``fetch`` is idempotent and verifies SHA-256 when the manifest pins one.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

MANIFEST_NAME = "artifacts.json"
MARKER_NAME = ".mcp-artifact.json"
ENV_ROOT = "MCP_ARTIFACTS_DIR"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_KINDS = {"model", "dataset", "other"}
_HERE = Path(__file__).resolve().parent


class ArtifactError(RuntimeError):
    """An artifact is unknown, unsupported, not installed, or failed to install."""


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def manifest_path() -> Path:
    return _HERE / MANIFEST_NAME


def validate_manifest(manifest: Any) -> list[str]:
    """Problems with a manifest (empty list when valid)."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]
    server = manifest.get("server")
    if not isinstance(server, str) or not _NAME_RE.match(server):
        errors.append("'server' must be the server name, e.g. 'retrochimera-mcp'")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return errors + ["'artifacts' must be a nonempty list"]
    names: set[str] = set()
    defaults: dict[str, int] = {}
    for i, art in enumerate(artifacts):
        where = f"artifacts[{i}]"
        if not isinstance(art, dict):
            errors.append(f"{where} must be an object")
            continue
        name = art.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append(f"{where}.name must match {_NAME_RE.pattern}")
        elif name in names:
            errors.append(f"duplicate artifact name {name!r}")
        else:
            names.add(name)
            where = f"artifact {name!r}"
        if art.get("kind") not in _KINDS:
            errors.append(f"{where}.kind must be one of {sorted(_KINDS)}")
        if not isinstance(art.get("description"), str) or not art["description"].strip():
            errors.append(f"{where}.description is required (what it is, when to choose it)")
        if not isinstance(art.get("source"), str) or not art["source"].startswith(("http://", "https://")):
            errors.append(f"{where}.source must be the URL documenting this artifact")
        used_by = art.get("used_by")
        if not isinstance(used_by, list) or not used_by or not all(isinstance(t, str) for t in used_by):
            errors.append(f"{where}.used_by must list the tools that use it")
        check = art.get("check")
        if not isinstance(check, list) or not check or not all(
            isinstance(c, str) and c and not Path(c).is_absolute() and ".." not in Path(c).parts
            for c in check
        ):
            errors.append(f"{where}.check must list relative paths present after installation")
        supported = art.get("supported", True)
        if supported is False:
            if not art.get("unsupported_reason"):
                errors.append(f"{where}.unsupported_reason is required when supported is false")
            if art.get("default"):
                errors.append(f"{where} cannot be default while unsupported")
        else:
            files = art.get("files")
            if not isinstance(files, list) or not files:
                errors.append(f"{where}.files must list at least one download")
            else:
                for j, f in enumerate(files):
                    if not isinstance(f, dict) or not str(f.get("url", "")).startswith(("http://", "https://")):
                        errors.append(f"{where}.files[{j}].url must be an http(s) URL")
                        continue
                    sha = f.get("sha256")
                    if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", str(sha)):
                        errors.append(f"{where}.files[{j}].sha256 must be 64 lowercase hex characters or null")
                    filename = f.get("filename")
                    if not isinstance(filename, str) or not filename or "/" in filename or filename.startswith("."):
                        errors.append(f"{where}.files[{j}].filename must be a plain file name")
        if art.get("default"):
            defaults[art.get("kind", "")] = defaults.get(art.get("kind", ""), 0) + 1
    for kind, count in defaults.items():
        if count > 1:
            errors.append(f"more than one default artifact of kind {kind!r}")
    return errors


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    path = path or manifest_path()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ArtifactError(f"artifact manifest not readable: {path}: {e.strerror}") from None
    except ValueError as e:
        raise ArtifactError(f"artifact manifest is not valid JSON: {path}: {e}") from None
    errors = validate_manifest(manifest)
    if errors:
        raise ArtifactError(f"invalid artifact manifest {path}: " + "; ".join(errors))
    return manifest


def _entry(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    for art in manifest["artifacts"]:
        if art["name"] == name:
            return art
    known = ", ".join(a["name"] for a in manifest["artifacts"])
    raise ArtifactError(f"unknown artifact {name!r}; available: {known}")


# ---------------------------------------------------------------------------
# Locations and status
# ---------------------------------------------------------------------------


def artifacts_root(manifest: dict[str, Any] | None = None) -> Path:
    """Directory holding this server's artifacts."""
    manifest = manifest or load_manifest()
    env = os.environ.get(ENV_ROOT, "").strip()
    if env:
        return Path(env).expanduser().resolve() / manifest["server"]
    return _HERE / "artifacts"


def artifact_dir(name: str, manifest: dict[str, Any] | None = None) -> Path:
    manifest = manifest or load_manifest()
    _entry(manifest, name)
    return artifacts_root(manifest) / name


def _missing_checks(art: dict[str, Any], directory: Path) -> list[str]:
    return [c for c in art["check"] if not (directory / c).exists()]


def is_installed(name: str, manifest: dict[str, Any] | None = None) -> bool:
    manifest = manifest or load_manifest()
    art = _entry(manifest, name)
    directory = artifacts_root(manifest) / name
    return (directory / MARKER_NAME).is_file() and not _missing_checks(art, directory)


def _fetch_hint(name: str) -> str:
    return f"python {_HERE / 'mcp_artifacts.py'} fetch {name}"


def resolve(name: str | None = None, kind: str | None = None) -> Path:
    """Installed directory of artifact ``name`` (or the default of ``kind``).

    Raises ArtifactError with the install command when it is missing, so the
    MCP client sees an actionable message instead of a stack trace.
    """
    manifest = load_manifest()
    if name is None:
        name = default_name(kind, manifest)
    art = _entry(manifest, name)
    if art.get("supported", True) is False:
        raise ArtifactError(f"artifact {name!r} is not supported by this server: {art['unsupported_reason']}")
    directory = artifacts_root(manifest) / name
    if not (directory / MARKER_NAME).is_file():
        raise ArtifactError(
            f"artifact {name!r} is not installed (expected at {directory}). "
            f"Install it with: {_fetch_hint(name)}"
        )
    missing = _missing_checks(art, directory)
    if missing:
        raise ArtifactError(
            f"artifact {name!r} at {directory} is incomplete (missing {', '.join(missing)}). "
            f"Reinstall it with: {_fetch_hint(name)} --force"
        )
    return directory


def default_name(kind: str | None = None, manifest: dict[str, Any] | None = None) -> str:
    manifest = manifest or load_manifest()
    for art in manifest["artifacts"]:
        if art.get("default") and (kind is None or art["kind"] == kind):
            return art["name"]
    raise ArtifactError(f"no default artifact{f' of kind {kind!r}' if kind else ''}; pass a name")


def list_artifacts() -> list[dict[str, Any]]:
    """Every declared artifact with its install status, for a list tool."""
    manifest = load_manifest()
    root = artifacts_root(manifest)
    rows = []
    for art in manifest["artifacts"]:
        directory = root / art["name"]
        supported = art.get("supported", True) is not False
        installed = supported and is_installed(art["name"], manifest)
        rows.append(
            {
                "name": art["name"],
                "kind": art["kind"],
                "description": art["description"],
                "default": bool(art.get("default")),
                "used_by": art["used_by"],
                "supported": supported,
                "unsupported_reason": None if supported else art["unsupported_reason"],
                "installed": installed,
                "path": str(directory) if installed else None,
                "install_command": None if installed or not supported else _fetch_hint(art["name"]),
                "source": art["source"],
                "license": art.get("license"),
                "size_bytes": art.get("size_bytes"),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _lock(path: Path):
    """Exclusive lock so concurrent deployers don't install the same artifact twice."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        except ImportError:  # non-POSIX: best effort
            pass
        yield


# Hosts whose browser-facing download links answer scripts with a bot
# challenge, mapped to the direct-download URL the host documents.
_DIRECT_DOWNLOAD = (
    (re.compile(r"^https?://(?:www\.)?figshare\.com/ndownloader/"), "https://ndownloader.figshare.com/"),
)


def direct_url(url: str) -> str:
    """The script-friendly form of a known download URL; other URLs unchanged."""
    for pattern, replacement in _DIRECT_DOWNLOAD:
        if pattern.match(url):
            return pattern.sub(replacement, url, count=1)
    return url


def _download(url: str, dest: Path, expected_sha256: str | None) -> str:
    fetch_url = direct_url(url)
    request = urllib.request.Request(fetch_url, headers={"User-Agent": "paper2mcp-artifacts/1"})
    digest = hashlib.sha256()
    part = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as out:
        # A bot challenge or "accepted, try later" page is not the file.
        if response.status != 200 or response.headers.get("x-amzn-waf-action"):
            out.close()
            part.unlink(missing_ok=True)
            raise ArtifactError(
                f"{fetch_url} returned HTTP {response.status} without the file"
                + (" (blocked by a bot challenge)" if response.headers.get("x-amzn-waf-action") else "")
                + "; use the host's direct-download URL in artifacts.json, or install with"
                " `adopt NAME --from DIR`"
            )
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    actual = digest.hexdigest()
    if expected_sha256 and actual != expected_sha256:
        part.unlink(missing_ok=True)
        raise ArtifactError(f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {actual}")
    part.replace(dest)
    return actual


def _is_archive(path: Path) -> bool:
    return any(path.name.endswith(ext) for ext in (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"))


def _flatten_single_top_dir(art: dict[str, Any], staging: Path) -> None:
    """Archives often wrap content in one folder; lift it when the checks live there."""
    if not _missing_checks(art, staging):
        return
    entries = [p for p in staging.iterdir() if p.name != MARKER_NAME]
    if len(entries) == 1 and entries[0].is_dir() and not _missing_checks(art, entries[0]):
        inner = entries[0]
        for child in inner.iterdir():
            child.rename(staging / child.name)
        inner.rmdir()


def _write_marker(directory: Path, art: dict[str, Any], hashes: dict[str, str], origin: str) -> None:
    (directory / MARKER_NAME).write_text(
        json.dumps({"name": art["name"], "origin": origin, "files": hashes}, indent=2) + "\n",
        encoding="utf-8",
    )


def _install(art: dict[str, Any], target: Path, populate) -> None:
    """Build in a staging dir next to ``target``, check it, then move into place."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{art['name']}-", dir=target.parent))
    try:
        hashes, origin = populate(staging)
        _flatten_single_top_dir(art, staging)
        missing = _missing_checks(art, staging)
        if missing:
            raise ArtifactError(f"artifact {art['name']!r} installed without expected paths: {', '.join(missing)}")
        _write_marker(staging, art, hashes, origin)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def fetch(name: str, force: bool = False) -> Path:
    manifest = load_manifest()
    art = _entry(manifest, name)
    if art.get("supported", True) is False:
        raise ArtifactError(f"artifact {name!r} is not supported by this server: {art['unsupported_reason']}")
    target = artifacts_root(manifest) / name
    with _lock(target.parent / f".{name}.lock"):
        if not force and is_installed(name, manifest):
            return target

        def populate(staging: Path) -> tuple[dict[str, str], str]:
            hashes: dict[str, str] = {}
            for spec in art["files"]:
                download = staging / spec["filename"]
                print(f"downloading {spec['url']} -> {download.name}", file=sys.stderr)
                hashes[spec["filename"]] = _download(spec["url"], download, spec.get("sha256"))
                if spec.get("extract", _is_archive(download)):
                    shutil.unpack_archive(str(download), str(staging))
                    download.unlink()
            return hashes, "fetch"

        _install(art, target, populate)
    return target


def adopt(name: str, source: Path) -> Path:
    """Install from an already extracted copy (e.g. downloaded while running tutorials).

    Files are hard-linked when possible, so a multi-GB checkpoint is not duplicated.
    """
    manifest = load_manifest()
    art = _entry(manifest, name)
    source = source.expanduser().resolve()
    if _missing_checks(art, source):
        raise ArtifactError(f"{source} does not look like artifact {name!r} (missing {', '.join(_missing_checks(art, source))})")
    target = artifacts_root(manifest) / name
    with _lock(target.parent / f".{name}.lock"):

        def populate(staging: Path) -> tuple[dict[str, str], str]:
            def link_or_copy(src: str, dst: str) -> None:
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)

            shutil.copytree(source, staging, dirs_exist_ok=True, copy_function=link_or_copy)
            return {}, f"adopted from {source}"

        _install(art, target, populate)
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print every artifact and its status as JSON")
    sub.add_parser("check", help="validate the manifest")
    p_fetch = sub.add_parser("fetch", help="download and install artifacts")
    p_fetch.add_argument("names", nargs="*")
    p_fetch.add_argument("--default", action="store_true", help="install every default artifact")
    p_fetch.add_argument("--force", action="store_true", help="reinstall even if present")
    p_adopt = sub.add_parser("adopt", help="install from an extracted copy")
    p_adopt.add_argument("name")
    p_adopt.add_argument("--from", dest="source", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "check":
            errors = validate_manifest(json.loads(manifest_path().read_text(encoding="utf-8")))
            print(json.dumps({"manifest": str(manifest_path()), "errors": errors}, indent=2))
            return 1 if errors else 0
        if args.command == "list":
            print(json.dumps(list_artifacts(), indent=2))
            return 0
        if args.command == "adopt":
            print(adopt(args.name, args.source))
            return 0
        manifest = load_manifest()
        names = list(args.names)
        if args.default:
            names += [a["name"] for a in manifest["artifacts"] if a.get("default")]
        if not names:
            parser.error("name artifacts to fetch, or pass --default")
        for name in dict.fromkeys(names):
            print(fetch(name, force=args.force))
        return 0
    except (ArtifactError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
