from __future__ import annotations

import json
import os
import re
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote, urljoin, urlparse
from urllib.request import Request, urlopen

from .credentials import (
    CLI_USER_AGENT,
    DEFAULT_REGISTRY,
    normalize_registry_origin,
    resolve_token,
)
from .pack import PackageError


DEFAULT_REGISTRY_URL = "https://api.biosimulant.com/api"
DEFAULT_REGISTRY_ORIGIN = f"https://{DEFAULT_REGISTRY}"
REGISTRY_URL_ENV = "BIOSIMULANT_REGISTRY_URL"
LEGACY_API_BASE_ENV = "BIOSIMULANT_API_BASE_URL"
LAB_CACHE_DIR_ENV = "BIOSIMULANT_LAB_CACHE_DIR"
REGISTRY_USER_AGENT = CLI_USER_AGENT
REGISTRY_JSON_ACCEPT = "application/json"
REGISTRY_PACKAGE_ACCEPT = "application/zip, application/octet-stream, */*"
_PACKAGE_NAME_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*(?:@[A-Za-z0-9][A-Za-z0-9.+_-]*)?$"
)
_REGISTRY_HOST_RE = re.compile(
    r"^(?:localhost(?::[0-9]+)?|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]+)?)$"
)


@dataclass(frozen=True)
class PackageReference:
    package_name: str
    version: str | None
    registry: str | None = None

    @property
    def qualified_name(self) -> str:
        prefix = f"{self.registry}/" if self.registry else ""
        suffix = f"@{self.version}" if self.version else ""
        return f"{prefix}{self.package_name}{suffix}"


class RegistryError(PackageError):
    """Raised when the public Biosimulant registry cannot satisfy a request."""


class PublicRegistryClient:
    """Registry API client supporting v1 discovery and legacy Hub endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        registry: str | None = None,
        token: str | None = None,
    ) -> None:
        value = (
            base_url
            or os.environ.get(REGISTRY_URL_ENV)
            or os.environ.get(LEGACY_API_BASE_ENV)
            or (normalize_registry_origin(registry) if registry else DEFAULT_REGISTRY_ORIGIN)
        )
        self.base_url = value.rstrip("/")
        parsed = urlparse(self.base_url)
        self.registry_origin = (
            DEFAULT_REGISTRY_ORIGIN
            if self.base_url == DEFAULT_REGISTRY_URL
            else normalize_registry_origin(
                f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else registry
            )
        )
        self.token = token if token is not None else resolve_token(self.registry_origin)
        self._discovered = bool(
            base_url
            or os.environ.get(REGISTRY_URL_ENV)
            or os.environ.get(LEGACY_API_BASE_ENV)
        )
        self._v1 = "/api/registry/v1" in self.base_url

    @classmethod
    def for_reference(
        cls,
        reference: PackageReference,
        *,
        base_url: str | None = None,
    ) -> "PublicRegistryClient":
        return cls(base_url, registry=reference.registry)

    def discover(self) -> dict[str, Any]:
        if self._discovered:
            return {
                "protocolVersion": "1" if self._v1 else "legacy",
                "apiBase": self.base_url,
                "capabilities": [],
            }
        discovery_url = f"{self.registry_origin}/.well-known/biosimulant-registry"
        payload = self._json_url("GET", discovery_url)
        protocol = str(payload.get("protocolVersion") or "")
        api_base = payload.get("apiBase")
        if protocol != "1" or not isinstance(api_base, str) or not api_base:
            raise RegistryError("Registry discovery returned an unsupported protocol")
        self.base_url = urljoin(f"{self.registry_origin}/", api_base).rstrip("/")
        self._v1 = True
        self._discovered = True
        return payload

    def _ensure_discovered(self) -> None:
        if not self._discovered:
            try:
                self.discover()
            except RegistryError:
                if self.registry_origin != DEFAULT_REGISTRY_ORIGIN:
                    raise
                # Migration compatibility: Hub discovery may be deployed after
                # this CLI. Preserve the existing public read API until then.
                self.base_url = DEFAULT_REGISTRY_URL
                self._v1 = False
                self._discovered = True

    def search_labs(
        self,
        query: str | None = None,
        *,
        page: int = 1,
        page_size: int = 20,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self._ensure_discovered()
        if self._v1:
            params_v1: list[tuple[str, str | int]] = [
                ("page", page),
                ("pageSize", page_size),
            ]
            if query:
                params_v1.append(("q", query))
            for tag in tags or []:
                params_v1.append(("tags", tag))
            return self._json("GET", "/packages", params=params_v1)
        params: list[tuple[str, str | int]] = [
            ("scope", "discover"),
            ("page", page),
            ("page_size", page_size),
        ]
        if query:
            params.append(("search", query))
        for tag in tags or []:
            params.append(("tags", tag))
        return self._json("GET", "/labs", params=params)

    def lab_info(self, reference: str) -> dict[str, Any]:
        parsed = parse_package_reference(reference, allow_missing_version=True)
        if parsed is not None:
            artifact = self.resolve_package(parsed.package_name, parsed.version)
            payload: dict[str, Any] = {
                "kind": "lab_package",
                "reference": reference,
                "artifact": artifact,
            }
            lab_id = artifact.get("lab_id")
            if lab_id:
                try:
                    payload["lab"] = self.get_lab(str(lab_id))
                except RegistryError:
                    payload["lab"] = None
            return payload
        return {
            "kind": "lab",
            "reference": reference,
            "lab": self.get_lab(reference),
        }

    def lab_versions(
        self,
        reference: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        parsed = parse_package_reference(reference, allow_missing_version=True)
        self._ensure_discovered()
        if self._v1 and parsed is not None:
            namespace, name = parsed.package_name.split("/", 1)
            return self._json(
                "GET",
                f"/packages/{quote(namespace, safe='')}/{quote(name, safe='')}/versions",
                params=[("page", page), ("page_size", page_size)],
            )
        lab_id = reference
        if parsed is not None:
            artifact = self.resolve_package(parsed.package_name, parsed.version)
            lab_id = str(artifact.get("lab_id") or "")
            if not lab_id:
                raise RegistryError(
                    f"Package {reference} is not linked to a downloadable lab"
                )
        return self._json(
            "GET",
            f"/labs/{quote(lab_id, safe='')}/versions",
            params=[("page", page), ("page_size", page_size)],
        )

    def resolve_package(
        self, package_name: str, version: str | None = None
    ) -> dict[str, Any]:
        self._ensure_discovered()
        if self._v1:
            namespace, name = package_name.split("/", 1)
            path = f"/packages/{quote(namespace, safe='')}/{quote(name, safe='')}"
            if version:
                path = f"{path}/{quote(version, safe='')}"
            payload = self._json("GET", path)
            latest = payload.get("latest")
            if version is None and isinstance(latest, dict):
                return latest
            return payload
        path = f"/packages/resolve/{quote(package_name, safe='/')}"
        params = [("version", version)] if version else None
        return self._json("GET", path, params=params)

    def get_lab(self, lab_id: str) -> dict[str, Any]:
        self._ensure_discovered()
        return self._json("GET", f"/labs/{quote(lab_id, safe='')}")

    def download_package(
        self,
        artifact_id: str,
        *,
        package_name: str | None = None,
        version: str | None = None,
    ) -> bytes:
        self._ensure_discovered()
        if self._v1:
            if not package_name or not version:
                raise RegistryError(
                    "Registry v1 downloads require package name and version"
                )
            namespace, name = package_name.split("/", 1)
            return self._bytes(
                "GET",
                f"/packages/{quote(namespace, safe='')}/{quote(name, safe='')}/"
                f"{quote(version, safe='')}/download",
                accept=REGISTRY_PACKAGE_ACCEPT,
            )
        return self._bytes(
            "GET",
            f"/packages/{quote(artifact_id, safe='')}/download",
            accept=REGISTRY_PACKAGE_ACCEPT,
        )

    def publish_package(
        self,
        package_file: str | Path,
        *,
        package_name: str,
        version: str,
        visibility: str = "private",
    ) -> dict[str, Any]:
        self._ensure_discovered()
        if not self._v1:
            raise RegistryError("Publishing requires Biosimulant Registry API v1")
        if not self.token:
            raise RegistryError("Registry authentication is required for publishing")
        path = Path(package_file).expanduser().resolve()
        if not path.is_file():
            raise RegistryError(f"Package file not found: {path}")
        content = path.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        return self._json(
            "POST",
            "/packages",
            body=content,
            content_type="application/octet-stream",
            headers={
                "X-Biosimulant-Filename": path.name,
                "X-Biosimulant-Package": package_name,
                "X-Biosimulant-Version": version,
                "X-Biosimulant-Visibility": visibility,
                "X-Biosimulant-Sha256": sha256,
            },
        )

    def _url(
        self, path: str, *, params: list[tuple[str, str | int | None]] | None = None
    ) -> str:
        url = f"{self.base_url}{path}"
        clean = [(key, value) for key, value in params or [] if value is not None]
        if clean:
            url = f"{url}?{urlencode(clean, doseq=True)}"
        return url

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str | int | None]] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = self._bytes(
            method,
            path,
            params=params,
            body=body,
            content_type=content_type,
            headers=headers,
        )
        return self._decode_json(data, path)

    def _json_url(self, method: str, url: str) -> dict[str, Any]:
        data = self._request_bytes(method, url)
        return self._decode_json(data, url)

    @staticmethod
    def _decode_json(data: bytes, path: str) -> dict[str, Any]:
        try:
            value = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RegistryError(f"Registry returned invalid JSON for {path}") from exc
        if not isinstance(value, dict):
            raise RegistryError(f"Registry returned non-object JSON for {path}")
        return _normalize_registry_payload(value)

    def _bytes(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str | int | None]] | None = None,
        accept: str = REGISTRY_JSON_ACCEPT,
        body: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return self._request_bytes(
            method,
            self._url(path, params=params),
            accept=accept,
            body=body,
            content_type=content_type,
            headers=headers,
        )

    def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        accept: str = REGISTRY_JSON_ACCEPT,
        body: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        request = Request(url, data=body, method=method)
        request.add_header("User-Agent", REGISTRY_USER_AGENT)
        request.add_header("Accept", accept)
        if content_type:
            request.add_header("Content-Type", content_type)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401:
                raise RegistryError(
                    "Registry item requires authentication (HTTP 401)"
                ) from exc
            if exc.code == 403:
                body_lower = body.lower()
                if "error code: 1010" in body_lower or (
                    "cloudflare" in body_lower and "1010" in body_lower
                ):
                    raise RegistryError(
                        "Registry request was blocked by Cloudflare browser integrity checks "
                        "(HTTP 403 / error 1010). Upgrade the Biosimulant CLI or ask the "
                        "registry operator to allow biosimulant-cli requests."
                    ) from exc
                raise RegistryError(
                    "Registry item is private or access is forbidden (HTTP 403)"
                ) from exc
            if exc.code == 404:
                raise RegistryError("Registry item was not found (HTTP 404)") from exc
            raise RegistryError(f"Registry request failed with HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RegistryError(f"Registry request failed: {exc.reason}") from exc


def parse_package_reference(
    value: str, *, allow_missing_version: bool = False
) -> PackageReference | None:
    raw = value.strip()
    if "/" not in raw or "\\" in raw:
        return None
    if raw.startswith((".", "/", "~")):
        return None
    reference_part, separator, version = raw.rpartition("@")
    if separator:
        if not reference_part or not version:
            raise RegistryError("Package reference must use namespace/name@version")
    else:
        reference_part = raw
        version = ""
    parts = reference_part.split("/")
    registry_name: str | None = None
    if len(parts) == 3 and (
        "." in parts[0] or ":" in parts[0] or parts[0] == "localhost"
    ):
        if not _REGISTRY_HOST_RE.match(parts[0]):
            return None
        registry_name = parts[0].lower()
        package_part = "/".join(parts[1:])
    elif len(parts) == 2:
        package_part = reference_part
    else:
        return None
    candidate = f"{package_part}@{version}" if separator else package_part
    if not _PACKAGE_NAME_RE.match(candidate):
        return None
    if not separator:
        if allow_missing_version:
            return PackageReference(package_part, None, registry_name)
        raise RegistryError("Package reference must use namespace/name@version")
    if not package_part or not version:
        raise RegistryError("Package reference must use namespace/name@version")
    return PackageReference(package_part, version, registry_name)


def _normalize_registry_payload(value: Any) -> Any:
    """Add stable snake-case aliases to Registry API v1 response objects."""

    if isinstance(value, list):
        return [_normalize_registry_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    aliases = {
        "artifactId": "id",
        "labId": "lab_id",
        "packageType": "package_type",
        "sizeBytes": "size_bytes",
        "downloadUrl": "download_url",
        "defaultVersion": "default_version",
        "createdAt": "created_at",
        "updatedAt": "updated_at",
    }
    normalized = {
        key: _normalize_registry_payload(item)
        for key, item in value.items()
    }
    for source, target in aliases.items():
        if source in normalized and target not in normalized:
            normalized[target] = normalized[source]
    return normalized


def lab_destination_for_reference(reference: str, target: str | Path | None) -> Path:
    if target is not None:
        return Path(target).expanduser().resolve()
    parsed = parse_package_reference(reference, allow_missing_version=True)
    name = reference
    if parsed is not None:
        name = parsed.package_name.rsplit("/", 1)[-1]
    return Path.cwd().joinpath(name).resolve()


def lab_cache_dir() -> Path:
    configured = os.environ.get(LAB_CACHE_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home().joinpath(".cache", "biosimulant", "labs").resolve()


def cached_lab_destination_for_reference(
    reference: str,
    artifact: dict[str, Any],
    *,
    cache_dir: str | Path | None = None,
) -> Path:
    parsed = parse_package_reference(reference, allow_missing_version=True)
    if parsed is None:
        raise RegistryError("Lab reference must use namespace/name[@version]")
    version = parsed.version or str(artifact.get("version") or "latest")
    artifact_id = str(artifact.get("id") or artifact.get("sha256") or "")[:12]
    raw = f"{parsed.package_name}@{version}"
    if artifact_id:
        raw = f"{raw}-{artifact_id}"
    slug = "".join(char.lower() if char.isalnum() else "-" for char in raw)
    slug = "-".join(part for part in slug.split("-") if part)
    root = Path(cache_dir).expanduser().resolve() if cache_dir else lab_cache_dir()
    return root / (slug or "lab")
