"""The OSDR client against a fake OSDR (listing + redirect to S3), no network."""

import httpx
import pytest

from app.config import SEED_DIR
from app.osdr.client import OSDRClient, OSDRError
from app.osdr.datasets import DATASETS_BY_KEY

SPEC = DATASETS_BY_KEY["cbc"]
CSV = (SEED_DIR / SPEC.file_name).read_bytes()


def fake_osdr(listing_ok=True, html=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/osdr/data/osd/files/569":
            if not listing_ok:
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "studies": {
                        "OSD-569": {
                            "study_files": [
                                {
                                    "file_name": SPEC.file_name,
                                    "remote_url": f"/geode-py/ws/studies/OSD-569/download?source=datamanager&file={SPEC.file_name}",
                                }
                            ]
                        }
                    }
                },
            )
        if request.url.path.endswith("/download"):
            return httpx.Response(302, headers={"Location": "https://s3.example.com/signed.csv"})
        if request.url.host == "s3.example.com":
            body = b"<!DOCTYPE html><title>Error</title>" if html else CSV
            return httpx.Response(200, content=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_download_follows_listing_and_redirect():
    with OSDRClient(transport=fake_osdr()) as client:
        content, url = client.download(SPEC)
    assert content == CSV
    assert "download?source=datamanager" in url


def test_falls_back_when_listing_fails():
    with OSDRClient(transport=fake_osdr(listing_ok=False)) as client:
        content, url = client.download(SPEC)
    assert content == CSV
    assert url == client.fallback_url(SPEC)


def test_rejects_html_instead_of_csv():
    with OSDRClient(transport=fake_osdr(html=True)) as client, pytest.raises(OSDRError):
        client.download(SPEC)
