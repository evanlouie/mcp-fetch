import argparse
import asyncio

from .server import serve


def main():
    """MCP Fetch Server - HTTP fetching functionality for MCP.

    Main entry point for the MCP fetch server that provides HTTP fetching
    functionality through the Model Context Protocol.
    """

    parser = argparse.ArgumentParser(
        description="give a model the ability to make web requests"
    )
    _ = parser.add_argument("--user-agent", type=str, help="Custom User-Agent string")
    _ = parser.add_argument(
        "--ignore-robots-txt",
        action="store_true",
        help="Ignore robots.txt restrictions",
    )
    _ = parser.add_argument(
        "--proxy-url", type=str, help="Proxy URL to use for requests"
    )

    args = parser.parse_args()
    user_agent: str | None = args.user_agent
    ignore_robots_txt: bool = args.ignore_robots_txt
    proxy_url: str | None = args.proxy_url
    asyncio.run(serve(user_agent, ignore_robots_txt, proxy_url))


if __name__ == "__main__":
    main()
