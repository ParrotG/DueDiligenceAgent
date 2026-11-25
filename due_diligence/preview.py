"""Display current-turn Google Search results in a temporary local browser view."""

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from secrets import token_urlsafe
from threading import Event, Thread

from .sources import public_url


def _render(results) -> bytes:
    sections = []
    for result in results:
        search_entry = result.metadata.search_entry_point
        suggestions = search_entry.rendered_content if search_entry else None
        links = []
        for chunk in result.metadata.grounding_chunks or []:
            web = chunk.web
            if web and web.uri and public_url(web.uri):
                links.append(
                    f'<li><a href="{escape(web.uri)}">'
                    f'{escape(web.title or web.uri)}</a></li>'
                )
        sections.append(
            f"<section><pre>{escape(result.text)}</pre>"
            f"<ul>{''.join(links)}</ul>{suggestions or ''}</section>"
        )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Google Search results</title>"
        "<style>body{font:16px system-ui;margin:2rem auto;max-width:70ch;"
        "padding:0 1rem}pre{white-space:pre-wrap;font:inherit}</style>"
        f"<h1>Google Search results</h1>{''.join(sections)}</html>"
    ).encode()


class SearchPreview:
    """Host one turn's results on a random loopback URL until closed."""

    def __init__(self, results):
        body = _render(results)
        path = f"/{token_urlsafe(24)}"
        self.viewed = Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                if handler.path != path:
                    handler.send_error(404)
                    return
                handler.send_response(200)
                handler.send_header("Content-Type", "text/html; charset=utf-8")
                handler.send_header("Content-Length", str(len(body)))
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Referrer-Policy", "no-referrer")
                handler.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                )
                handler.end_headers()
                handler.wfile.write(body)
                self.viewed.set()

            def log_message(handler, format, *args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}{path}"
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop serving the transient response and release the port."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
