"""HTTP client for the NASA Open Science Data Repository (OSDR)."""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from app.osdr.datasets import DatasetSpec

log = logging.getLogger(__name__)


class OSDRError(RuntimeError):
    pass


class OSDRClient:
    """Resolves a dataset's download URL through the study file listing and downloads it.

    The listing (``/osdr/data/osd/files/<n>``) is the source of truth for the
    ``remote_url``; if it is unavailable or the file is not listed, the client
    falls back to the documented ``geode-py`` download URL. Downloads redirect
    to a signed S3 URL, so redirects are followed.
    """

    def __init__(
        self,
        base_url: str = "https://osdr.nasa.gov",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": "crew-health-api/1.0 (+NASA Space Apps)"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OSDRClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def list_files(self, study_number: int) -> list[dict]:
        url = f"{self.base_url}/osdr/data/osd/files/{study_number}"
        response = self._client.get(url)
        response.raise_for_status()
        payload = response.json()
        study = payload.get("studies", {}).get(f"OSD-{study_number}", {})
        return study.get("study_files", [])

    def fallback_url(self, spec: DatasetSpec) -> str:
        return (
            f"{self.base_url}/geode-py/ws/studies/{spec.study.id}/download"
            f"?source=datamanager&file={quote(spec.file_name)}"
        )

    def resolve_download_url(self, spec: DatasetSpec) -> str:
        try:
            for entry in self.list_files(spec.study.number):
                if entry.get("file_name") == spec.file_name and entry.get("remote_url"):
                    remote = entry["remote_url"]
                    return remote if remote.startswith("http") else f"{self.base_url}{remote}"
            log.warning("%s not found in the %s file listing", spec.file_name, spec.study.id)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Could not read the %s file listing: %s", spec.study.id, exc)
        return self.fallback_url(spec)

    def download(self, spec: DatasetSpec) -> tuple[bytes, str]:
        url = self.resolve_download_url(spec)
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OSDRError(f"Download failed for {spec.file_name}: {exc}") from exc
        content = response.content
        head = content[:200].lstrip(b"\xef\xbb\xbf").lower()
        if head.startswith(b"<!doctype") or head.startswith(b"<html"):
            raise OSDRError(f"OSDR returned HTML instead of CSV for {spec.file_name}")
        return content, url
