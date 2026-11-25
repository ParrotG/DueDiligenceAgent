"""Fetch original public documents through HTTPX."""

import asyncio
from html.parser import HTMLParser
from io import BytesIO
from ipaddress import ip_address
from socket import SOCK_STREAM
from urllib.parse import urljoin, urlsplit

import httpx
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .config import SourcesConfig
from .sources import public_url


class PageText(HTMLParser):
    """Extract readable HTML while omitting script and style blocks."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden.append(tag)
        elif not self.hidden and tag in {"p", "li", "div", "section", "h1", "h2", "h3", "br", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(line for row in "".join(self.parts).splitlines()
                         if (line := " ".join(row.split())))


async def resolve_public_address(host: str, port: int) -> str:
    """Resolve a hostname before fetching so the connection can use the vetted IP."""
    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=SOCK_STREAM)
    addresses = [ip_address(record[4][0]) for record in records]
    if not addresses or any(
        not address.is_global or address.is_multicast or address.is_reserved
        or getattr(address, "ipv4_mapped", None) or getattr(address, "sixtofour", None)
        or getattr(address, "teredo", None)
        for address in addresses
    ):
        raise ValueError("The address does not resolve exclusively to public hosts.")
    return str(min(addresses, key=lambda address: address.version))


def pdf_page(body: bytes, page: int) -> tuple[str, int]:
    """Extract the requested page and retain its one-based locator."""
    reader = PdfReader(BytesIO(body))
    total = len(reader.pages)
    if page > total:
        raise ValueError("The page number is outside the document.")
    return reader.pages[page - 1].extract_text(), total


async def fetch_document(
    url: str, settings: SourcesConfig, *, page: int = 1, offset: int = 0, raw: bool = False
) -> dict:
    """Return a bounded slice of an original document or a structured retrieval status."""
    if page < 1 or offset < 0:
        return {"status": "invalid_locator"}
    try:
        async with asyncio.timeout(settings.fetch_timeout_seconds):
            return await _fetch(url, settings, page, offset, raw)
    except (TimeoutError, httpx.TimeoutException):
        return {"status": "fetch_timeout", "url": url}
    except (httpx.RequestError, OSError):
        return {"status": "fetch_failed", "url": url}
    except (ValueError, PyPdfError, LookupError, RecursionError):
        return {"status": "invalid_target_or_document", "url": url}


async def _fetch(url, settings, page, offset, raw):
    limits = httpx.Limits(max_keepalive_connections=0)
    async with httpx.AsyncClient(trust_env=False, limits=limits) as client:
        for _ in range(settings.fetch_max_redirects + 1):
            checked = public_url(url) if len(url) <= settings.max_url_chars else None
            if checked is None:
                return {"status": "invalid_public_url"}
            url = checked
            parsed = urlsplit(url)
            host = httpx.URL(url).raw_host.decode("ascii")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            address = await resolve_public_address(host, port)
            target = httpx.URL(url).copy_with(host=address)
            # Pin DNS while preserving HTTP virtual hosting and TLS certificate validation.
            headers = {
                "Host": parsed.netloc if ":" in host else (
                    f"{host}:{parsed.port}" if parsed.port else host
                ),
                "Accept-Encoding": "identity",
                "User-Agent": "DueDiligenceAgent/0.1 (public document research)",
            }
            client.cookies.clear()
            async with client.stream(
                "GET", target, headers=headers, extensions={"sni_hostname": host},
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return {"status": "invalid_redirect", "url": url}
                    url = urljoin(url, location)
                    continue
                if not response.is_success:
                    return {"status": "http_error", "url": url, "http_status": response.status_code}
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return {"status": "unsupported_encoding", "url": url}
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                body = bytearray()
                async for chunk in response.aiter_raw(chunk_size=65536):
                    if len(body) + len(chunk) > settings.fetch_max_bytes:
                        return {"status": "document_too_large", "url": url}
                    body.extend(chunk)
                encoding = response.encoding or "utf-8"
            return await _excerpt(url, bytes(body), content_type, encoding, settings, page, offset, raw)
    return {"status": "redirect_limit"}


async def _excerpt(url, body, content_type, encoding, settings, page, offset, raw):
    result = {"status": "retrieved", "url": url, "content_type": content_type}
    if content_type == "application/pdf" or body.startswith(b"%PDF-"):
        text, total = await asyncio.to_thread(pdf_page, body, page)
        result.update(page=page, total_pages=total)
    elif (content_type.startswith("text/") or content_type.endswith(("+json", "+xml"))
          or content_type in {"application/json", "application/xml", "application/xhtml+xml"}):
        text = body.decode(encoding, errors="replace")
        if content_type in {"text/html", "application/xhtml+xml"} and not raw:
            parser = PageText()
            parser.feed(text)
            parser.close()
            text = parser.text()
    else:
        return {"status": "unsupported_content_type", "url": url, "content_type": content_type}
    end = offset + settings.fetch_max_chars
    result.update(text=text[offset:end], offset=offset, next_offset=end if end < len(text) else None)
    return result
