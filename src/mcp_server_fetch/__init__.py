import argparse
import asyncio

from .server import serve


def main():
    """MCP Fetch Server - HTTP fetching functionality for MCP.

    Main entry point for the MCP fetch server that provides HTTP fetching
    functionality through the Model Context Protocol with browser impersonation.
    """

    parser = argparse.ArgumentParser(
        description="give a model the ability to make web requests with browser impersonation"
    )
    _ = parser.add_argument(
        "--proxy-url", type=str, help="Proxy URL to use for requests"
    )

    args = parser.parse_args()
    proxy_url: str | None = args.proxy_url
    asyncio.run(serve(proxy_url))


if __name__ == "__main__":
    main()
