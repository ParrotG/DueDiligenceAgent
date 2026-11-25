"""Interactive and one-shot command-line access to the research assistant."""

import argparse
import asyncio
import sys
from contextlib import closing

import httpx
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.genai import errors

from .preview import SearchPreview
from .config import load_settings
from .runtime import ResearchAssistant
from .sources import source_error


def terminal_text(text: str) -> str:
    """Filter control characters from model output before terminal display."""
    return "".join(char for char in text if char.isprintable() or char in "\n\t")


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    assistant = ResearchAssistant(args.model, settings=settings)

    async def respond(message: str) -> bool:
        try:
            turn = await assistant.ask(message)
        except (errors.APIError, httpx.RequestError) as error:
            print(f"Error: {source_error(error)}", file=sys.stderr)
            return False
        except LlmCallsLimitExceededError:
            print("Error: The turn reached its model call limit.", file=sys.stderr)
            return False
        if args.trace:
            print(f"Workflow: {' -> '.join(turn.stages)}", file=sys.stderr)
            calls = " -> ".join(turn.calls) or "none"
            print(f"Tools: {calls}", file=sys.stderr)
        print(terminal_text(turn.answer) or "The agent returned no answer.")
        if turn.searches and (sys.stdin.isatty() or args.preview_search):
            try:
                with closing(SearchPreview(turn.searches)) as server:
                    print(f"Google Search results: {server.url}", file=sys.stderr, flush=True)
                    if sys.stdin.isatty():
                        await asyncio.to_thread(input, "Press Enter when finished viewing: ")
                    else:
                        await asyncio.to_thread(server.viewed.wait, settings.sources.preview_wait_seconds)
            except OSError:
                print("Could not open the local Google Search preview.", file=sys.stderr)
        elif turn.searches:
            print("Use --preview-search to view Google Search results.", file=sys.stderr)
        return bool(turn.answer)

    try:
        if args.prompt is not None:
            return 0 if await respond(args.prompt) else 1
        print("Due Diligence Agent. Enter /reset for a new case or /quit to exit.")
        while True:
            message = (await asyncio.to_thread(input, "You> ")).strip()
            if message == "/quit":
                return 0
            if message == "/reset":
                await assistant.close()
                assistant = ResearchAssistant(args.model, settings=settings)
                print("New case started.")
            elif message:
                await respond(message)
    except EOFError:
        return 0
    finally:
        await assistant.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Send one request and exit.")
    parser.add_argument("--model", help="Override the model in config.toml.")
    parser.add_argument("--trace", action="store_true", help="Show tool names on stderr.")
    parser.add_argument(
        "--preview-search",
        action="store_true",
        help="In batch mode, wait for the local search preview to open.",
    )
    args = parser.parse_args()
    if args.prompt is not None and not args.prompt.strip():
        parser.error("--prompt must contain a research request.")
    try:
        return asyncio.run(run(args))
    except (ValueError, OSError) as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
