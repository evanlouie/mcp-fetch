from typing import Annotated, Any, cast
from urllib.parse import urlparse, urlunparse

import markdownify
import readabilipy.simple_json
from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests.exceptions import RequestException
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    ErrorData,
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
    Tool,
)
from pydantic import BaseModel, Field, HttpUrl, ValidationError


class GetPromptArguments(BaseModel):
    url: HttpUrl


def extract_content_from_html(html: str) -> str:
    """Extract and convert HTML content to Markdown format.

    :param html: Raw HTML content to process
    :returns: Simplified markdown version of the content
    """
    ret = readabilipy.simple_json.simple_json_from_html_string(
        html, use_readability=True
    )
    if not ret["content"]:
        return "<error>Page failed to be simplified from HTML</error>"
    content = markdownify.markdownify(ret["content"], heading_style=markdownify.ATX)  # pyright: ignore[reportUnknownMemberType]
    return cast(str, content)


def get_robots_txt_url(url: str) -> str:
    """Get the robots.txt URL for a given website URL.

    :param url: Website URL to get robots.txt for
    :returns: URL of the robots.txt file
    """
    # Parse the URL into components
    parsed = urlparse(url)

    # Reconstruct the base URL with just scheme, netloc, and /robots.txt path
    robots_url = urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))

    return robots_url


async def fetch_url(
    url: str, force_raw: bool = False, proxy_url: str | None = None
) -> tuple[str, str]:
    """Fetch the URL and return the content in a form ready for the LLM.

    :param url: URL to fetch
    :param force_raw: Whether to return raw content without HTML simplification
    :param proxy_url: Optional proxy URL to use for the request
    :returns: Tuple of (content, prefix) where content is the processed page content
              and prefix is a status message string
    :raises McpError: If the request fails or returns an error status code
    """

    async with AsyncSession[Response](impersonate="chrome") as session:
        try:
            response = await session.get(
                url,
                allow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                },
                timeout=30,
                proxy=proxy_url,
            )
        except RequestException as e:
            raise McpError(
                ErrorData(code=INTERNAL_ERROR, message=f"Failed to fetch {url}: {e!r}")
            )

        if response.status_code >= 400:
            raise McpError(
                ErrorData(
                    code=INTERNAL_ERROR,
                    message=f"Failed to fetch {url} - status code {response.status_code}",
                )
            )

        page_raw = response.text

    content_type = cast(str, response.headers.get("content-type", ""))
    is_page_html = (
        "<html" in page_raw[:100] or "text/html" in content_type or not content_type
    )

    if is_page_html and not force_raw:
        return extract_content_from_html(page_raw), ""

    return (
        page_raw,
        f"Content type {content_type} cannot be simplified to markdown, but here is the raw content:\n",
    )


class Fetch(BaseModel):
    """Parameters for fetching a URL.

    This model defines the parameters that can be passed to the fetch tool
    for controlling how URLs are retrieved and processed.
    """

    url: Annotated[HttpUrl, Field(description="URL to fetch")]
    max_length: Annotated[
        int,
        Field(
            default=5000,
            description="Maximum number of characters to return.",
            gt=0,
            lt=1000000,
        ),
    ]
    start_index: Annotated[
        int,
        Field(
            default=0,
            description="On return output starting at this character index, useful if a previous fetch was truncated and more context is required.",
            ge=0,
        ),
    ]
    raw: Annotated[
        bool,
        Field(
            default=False,
            description="Get the actual HTML content of the requested page, without simplification.",
        ),
    ]


async def serve(
    proxy_url: str | None = None,
) -> None:
    """Run the fetch MCP server.

    :param proxy_url: Optional proxy URL to use for requests
    """
    server = Server("mcp-fetch")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available tools for the MCP server.

        :returns: List of Tool objects defining the fetch tool
        """
        return [
            Tool(
                name="fetch",
                description="""Fetches a URL from the internet and optionally extracts its contents as markdown.

This tool uses Chrome browser impersonation to access websites that might otherwise block automated requests, making it more reliable for fetching content from various sources.""",
                inputSchema=Fetch.model_json_schema(),
            )
        ]

    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        """List available prompts for the MCP server.

        :returns: List of Prompt objects defining the fetch prompt
        """
        return [
            Prompt(
                name="fetch",
                description="Fetch a URL and extract its contents as markdown using Chrome browser impersonation",
                arguments=[
                    PromptArgument(
                        name="url", description="URL to fetch", required=True
                    )
                ],
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle tool calls for the fetch tool.

        :param name: Name of the tool being called
        :param arguments: Dictionary of arguments for the tool call
        :returns: List of TextContent objects with the fetched content
        :raises McpError: If arguments are invalid or fetching fails
        """
        try:
            args = Fetch.model_validate(arguments)
        except ValidationError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

        url = str(args.url)
        if not url:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="URL is required"))

        content, prefix = await fetch_url(url, force_raw=args.raw, proxy_url=proxy_url)
        original_length = len(content)
        if args.start_index >= original_length:
            content = "<error>No more content available.</error>"
        else:
            truncated_content = content[
                args.start_index : args.start_index + args.max_length
            ]
            if not truncated_content:
                content = "<error>No more content available.</error>"
            else:
                content = truncated_content
                actual_content_length = len(truncated_content)
                remaining_content = original_length - (
                    args.start_index + actual_content_length
                )
                # Only add the prompt to continue fetching if there is still remaining content
                if actual_content_length == args.max_length and remaining_content > 0:
                    next_start = args.start_index + actual_content_length
                    content += f"\n\n<error>Content truncated. Call the fetch tool with a start_index of {next_start} to get more content.</error>"
        return [TextContent(type="text", text=f"{prefix}Contents of {url}:\n{content}")]

    @server.get_prompt()
    async def get_prompt(
        name: str, arguments: dict[str, Any] | None
    ) -> GetPromptResult:
        """Handle prompt requests for manual URL fetching.

        :param name: Name of the prompt being requested
        :param arguments: Optional dictionary of arguments including the URL
        :returns: GetPromptResult with the fetched content or error message
        """

        try:
            args = GetPromptArguments.model_validate(arguments)
        except ValidationError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

        url = str(args.url)
        try:
            content, prefix = await fetch_url(url, proxy_url=proxy_url)
            # TODO: after SDK bug is addressed, don't catch the exception
        except McpError as e:
            return GetPromptResult(
                description=f"Failed to fetch {url}",
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(type="text", text=str(e)),
                    )
                ],
            )
        return GetPromptResult(
            description=f"Contents of {url}",
            messages=[
                PromptMessage(
                    role="user", content=TextContent(type="text", text=prefix + content)
                )
            ],
        )

    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options, raise_exceptions=True)
