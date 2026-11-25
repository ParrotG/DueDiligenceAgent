"""Verify public-document retrieval, locators, and request boundaries."""

from io import BytesIO
from socket import AF_INET, SOCK_STREAM
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx
from pypdf import PdfWriter

from due_diligence.config import load_settings
from due_diligence.http_sources import fetch_document, resolve_public_address
from due_diligence.research_skills import ResearchSkills


class HttpSourceTests(IsolatedAsyncioTestCase):
    async def fetch(self, handler, *, resolver=None, **options):
        if resolver is None:
            resolver = AsyncMock(return_value="93.184.215.14")
        settings = load_settings().sources.model_copy(update={"fetch_max_chars": 15})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("due_diligence.http_sources.httpx.AsyncClient", return_value=client):
            with patch("due_diligence.http_sources.resolve_public_address", resolver):
                return await fetch_document("https://example.com/original", settings, **options)

    async def test_validates_redirect_and_connects_to_vetted_address(self):
        seen = []

        def handler(request):
            seen.append((request.url.host, request.headers["Host"],
                         request.extensions["sni_hostname"], request.url.path))
            if request.url.path == "/original":
                return httpx.Response(
                    302, headers={"location": "https://example.org/final"}, stream=httpx.ByteStream(b""),
                )
            return httpx.Response(
                200, headers={"content-type": "text/html; charset=utf-8"},
                stream=httpx.ByteStream(b"<p>Original annual report</p><script>Hidden</script>"),
            )

        resolver = AsyncMock(side_effect=["93.184.215.14", "1.1.1.1"])
        result = await self.fetch(handler, resolver=resolver)
        self.assertEqual(result["text"], "Original annual")
        self.assertEqual(result["next_offset"], 15)
        self.assertEqual(result["url"], "https://example.org/final")
        self.assertEqual(seen, [
            ("93.184.215.14", "example.com", "example.com", "/original"),
            ("1.1.1.1", "example.org", "example.org", "/final"),
        ])

    async def test_private_redirect_is_not_fetched(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                302, headers={"location": "http://127.0.0.1/"}, stream=httpx.ByteStream(b""),
            )

        result = await self.fetch(handler)
        self.assertEqual(result["status"], "invalid_public_url")
        self.assertEqual(len(requests), 1)

    async def test_private_dns_result_is_rejected(self):
        answers = [
            (AF_INET, SOCK_STREAM, 0, "", ("1.1.1.1", 443)),
            (AF_INET, SOCK_STREAM, 0, "", ("127.0.0.1", 443)),
        ]
        with patch("asyncio.base_events.BaseEventLoop.getaddrinfo", new_callable=AsyncMock) as dns:
            dns.return_value = answers
            with self.assertRaises(ValueError):
                await resolve_public_address("example.com", 443)

    async def test_pdf_page_locator_is_retained(self):
        document = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.add_blank_page(width=72, height=72)
        writer.write(document)

        def handler(request):
            return httpx.Response(
                200, headers={"content-type": "application/pdf"},
                stream=httpx.ByteStream(document.getvalue()),
            )

        async def extract_pdf(function, *args):
            return function(*args)

        with patch("due_diligence.http_sources.asyncio.to_thread", side_effect=extract_pdf):
            result = await self.fetch(handler, page=2)
        self.assertEqual((result["status"], result["page"], result["total_pages"]),
                         ("retrieved", 2, 2))


class SkillTests(TestCase):
    def test_only_catalogued_skills_can_be_loaded(self):
        skills = ResearchSkills()
        self.assertIn("cited-diligence-report", skills.catalogue())
        self.assertIn("instructions", skills.load_research_skill("iterative-research"))
        self.assertEqual(skills.load_research_skill("../.env.local")["status"], "unknown_skill")
