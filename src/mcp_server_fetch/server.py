import contextlib
from typing import Annotated, Any, NamedTuple
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

from .session_manager import SessionConfig, SessionManager, SessionManagerError
from .ssrf_validator import SSRFValidator

# Constants for HTTP status codes and content inspection
HTTP_CLIENT_ERROR_THRESHOLD = 400
HTTP_SERVER_ERROR_THRESHOLD = 500
CONTENT_INSPECTION_SIZE = 2000
# Limit total response body read into memory (bytes)
MAX_RESPONSE_BODY_SIZE = 2_000_000  # ~2 MB

# Default user agent string
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class ProcessedContent(NamedTuple):
    """Processed content with metadata."""

    content: str
    prefix: str


class HttpConfig(BaseModel):
    """Configuration for HTTP requests."""

    proxy_url: str | None = Field(default=None, description="Optional proxy URL")
    timeout: int = Field(
        default=30, ge=1, le=300, description="Request timeout in seconds"
    )
    user_agent: str = Field(default=DEFAULT_USER_AGENT, description="User agent string")


class HttpResponse(BaseModel):
    """HTTP response data."""

    content: str = Field(description="Response content")
    status_code: int = Field(ge=100, le=599, description="HTTP status code")
    headers: dict[str, str] = Field(description="Response headers")
    url: str = Field(description="Final URL after redirects")


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
    content = markdownify.markdownify(ret["content"], heading_style=markdownify.ATX)
    return content


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


def _filter_headers(headers) -> dict[str, str]:
    """Filter out None values from headers dictionary.

    :param headers: Headers dictionary potentially containing None values
    :returns: Headers dictionary with only string values
    """
    return {k: v for k, v in headers.items() if v is not None}


async def _execute_http_request(
    url: str,
    session: AsyncSession[Response],
    config: HttpConfig,
    ssrf_validator: SSRFValidator,
) -> HttpResponse:
    """Execute HTTP request with SSRF protection.

    :param url: URL to fetch
    :param session: HTTP session to use
    :param config: HTTP configuration
    :param ssrf_validator: SSRF protection validator
    :returns: HTTP response data
    :raises RequestException: If request fails
    """
    response = await ssrf_validator.follow_redirects_safely(
        session,
        url,
        headers={"User-Agent": config.user_agent},
        timeout=config.timeout,
        proxy=config.proxy_url,
        stream=True,
    )

    try:
        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            with contextlib.suppress(ValueError):
                if int(declared_length) > MAX_RESPONSE_BODY_SIZE:
                    raise McpError(
                        ErrorData(
                            code=INTERNAL_ERROR,
                            message=(
                                f"Failed to fetch {url}: response exceeded "
                                f"{MAX_RESPONSE_BODY_SIZE} byte limit"
                            ),
                        )
                    )

        collected_chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_content(chunk_size=65536):
            total += len(chunk)
            if total > MAX_RESPONSE_BODY_SIZE:
                raise McpError(
                    ErrorData(
                        code=INTERNAL_ERROR,
                        message=(
                            f"Failed to fetch {url}: response exceeded "
                            f"{MAX_RESPONSE_BODY_SIZE} byte limit"
                        ),
                    )
                )
            collected_chunks.append(chunk)
        body_bytes = b"".join(collected_chunks)
    finally:
        with contextlib.suppress(Exception):
            await response.aclose()

    encoding = response.encoding or "utf-8"
    try:
        text = body_bytes.decode(encoding, errors="replace")
    except LookupError:
        text = body_bytes.decode("utf-8", errors="replace")

    return HttpResponse(
        content=text,
        status_code=response.status_code,
        headers=_filter_headers(response.headers),
        url=response.url,
    )


def _validate_http_response(response: HttpResponse) -> None:
    """Validate HTTP response status.

    :param response: HTTP response to validate
    :raises McpError: If status indicates error
    """
    if response.status_code >= HTTP_CLIENT_ERROR_THRESHOLD:
        # Distinguish between client errors (4xx) and server errors (5xx)
        if response.status_code >= HTTP_SERVER_ERROR_THRESHOLD:
            error_type = "Server error"
        else:
            error_type = "Client error"

        raise McpError(
            ErrorData(
                code=INTERNAL_ERROR,
                message=f"{error_type}: Failed to fetch {response.url}: HTTP {response.status_code}",
            )
        )


def _process_response_content(
    response: HttpResponse, force_raw: bool
) -> ProcessedContent:
    """Process response content based on type.

    :param response: HTTP response containing content
    :param force_raw: Whether to skip HTML processing
    :returns: ProcessedContent with content and prefix message
    """
    content_type = response.headers.get("content-type", "").lower()
    head_lower = response.content[:CONTENT_INSPECTION_SIZE].lower()

    is_html = (
        "text/html" in content_type
        or "application/xhtml+xml" in content_type
        or "<!doctype html" in head_lower
        or "<html" in head_lower
    )

    if is_html and not force_raw:
        return ProcessedContent(
            content=extract_content_from_html(response.content), prefix=""
        )

    return ProcessedContent(
        content=response.content,
        prefix=f"Content type {content_type} cannot be simplified to markdown, but here is the raw content:\n",
    )


async def fetch_url_with_fallback(
    url: str,
    session_manager: SessionManager,
    force_raw: bool = False,
    proxy_url: str | None = None,
) -> tuple[str, str]:
    """Fetch URL with session manager, falling back to per-request session on failure.

    :param url: URL to fetch
    :param session_manager: SessionManager instance for connection pooling
    :param force_raw: Whether to return raw content without HTML simplification
    :param proxy_url: Optional proxy URL to use for the request
    :returns: Tuple of (content, prefix) where content is the processed page content
              and prefix is a status message string
    :raises McpError: If the request fails or returns an error status code
    """
    try:
        return await fetch_url_pooled(url, session_manager, force_raw, proxy_url)
    except SessionManagerError:
        # Fallback to per-request session
        return await fetch_url_legacy(url, force_raw, proxy_url)


async def fetch_url_pooled(
    url: str,
    session_manager: SessionManager,
    force_raw: bool = False,
    proxy_url: str | None = None,
) -> tuple[str, str]:
    """Fetch URL using SessionManager for connection pooling.

    :param url: URL to fetch
    :param session_manager: SessionManager instance for connection pooling
    :param force_raw: Whether to return raw content without HTML simplification
    :param proxy_url: Optional proxy URL to use for the request
    :returns: Tuple of (content, prefix) where content is the processed page content
              and prefix is a status message string
    :raises McpError: If the request fails or returns an error status code
    """
    # Create SSRF validator and HTTP config
    ssrf_validator = SSRFValidator()
    http_config = HttpConfig(proxy_url=proxy_url)

    # Create session configuration
    session_config = SessionConfig(
        impersonate="chrome",
        proxy_url=proxy_url,
    )

    try:
        # Get session from manager
        session = await session_manager.get_session(session_config)

        attempt = 0
        while True:
            attempt += 1
            try:
                # Execute HTTP request
                response = await _execute_http_request(
                    url, session, http_config, ssrf_validator
                )
                break
            except RequestException as e:
                should_recreate = await session_manager.handle_request_error(
                    session_config, e
                )
                if should_recreate and attempt < 2:
                    session = await session_manager.get_session(session_config)
                    continue
                raise McpError(
                    ErrorData(
                        code=INTERNAL_ERROR, message=f"Failed to fetch {url}: {e!r}"
                    )
                ) from e

        # Validate response and process content
        _validate_http_response(response)
        processed = _process_response_content(response, force_raw)
        return processed.content, processed.prefix

    except SessionManagerError as e:
        raise SessionManagerError(f"Session manager failed: {e}") from e


async def fetch_url_legacy(
    url: str, force_raw: bool = False, proxy_url: str | None = None
) -> tuple[str, str]:
    """Legacy fetch URL implementation using per-request sessions.

    This is used as fallback when SessionManager fails.

    :param url: URL to fetch
    :param force_raw: Whether to return raw content without HTML simplification
    :param proxy_url: Optional proxy URL to use for the request
    :returns: Tuple of (content, prefix) where content is the processed page content
              and prefix is a status message string
    :raises McpError: If the request fails or returns an error status code
    """
    # Create SSRF validator and HTTP config
    ssrf_validator = SSRFValidator()
    http_config = HttpConfig(proxy_url=proxy_url)

    async with AsyncSession[Response](impersonate="chrome") as session:
        try:
            # Execute HTTP request
            response = await _execute_http_request(
                url, session, http_config, ssrf_validator
            )

            # Validate response and process content
            _validate_http_response(response)
            processed = _process_response_content(response, force_raw)
            return processed.content, processed.prefix

        except RequestException as e:
            raise McpError(
                ErrorData(code=INTERNAL_ERROR, message=f"Failed to fetch {url}: {e!r}")
            )


# Backward compatibility wrapper - will be used by serve() function
async def fetch_url(
    url: str, force_raw: bool = False, proxy_url: str | None = None
) -> tuple[str, str]:
    """Backward compatibility wrapper for fetch_url.

    This function is used by the serve() function handlers and will be updated
    to use SessionManager via dependency injection.
    """
    # This will be replaced by the session manager when serve() is updated
    return await fetch_url_legacy(url, force_raw, proxy_url)


# Backward compatibility wrapper for existing process_content function
def process_content(
    page_raw: str, response: Response, force_raw: bool
) -> tuple[str, str]:
    """Legacy content processing function for backward compatibility.

    :param page_raw: Raw page content
    :param response: HTTP response object
    :param force_raw: Whether to skip HTML processing
    :returns: Tuple of (processed_content, prefix_message)
    """
    # Convert to HttpResponse format and use new processing function
    http_response = HttpResponse(
        content=page_raw,
        status_code=response.status_code,
        headers=_filter_headers(response.headers),
        url=response.url,
    )
    processed = _process_response_content(http_response, force_raw)
    return processed.content, processed.prefix


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

    # Create SessionManager for connection pooling
    session_manager = SessionManager()

    async def fetch_with_session_manager(
        url: str, force_raw: bool = False, proxy_url_override: str | None = None
    ) -> tuple[str, str]:
        """Fetch URL using SessionManager with fallback."""
        effective_proxy_url = proxy_url_override or proxy_url
        return await fetch_url_with_fallback(
            url, session_manager, force_raw, effective_proxy_url
        )

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

        content, prefix = await fetch_with_session_manager(
            url, force_raw=args.raw, proxy_url_override=proxy_url
        )
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
        return [TextContent(type="text", text=f"{prefix}Contents:\n{content}")]

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
            content, prefix = await fetch_with_session_manager(
                url, proxy_url_override=proxy_url
            )
            # TODO: after SDK bug is addressed, don't catch the exception
        except McpError as e:
            return GetPromptResult(
                description="Failed to fetch URL",
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(type="text", text=str(e)),
                    )
                ],
            )
        return GetPromptResult(
            description="Contents of requested URL",
            messages=[
                PromptMessage(
                    role="user", content=TextContent(type="text", text=prefix + content)
                )
            ],
        )

    options = server.create_initialization_options()

    # Use AsyncExitStack for proper cleanup of SessionManager
    async with contextlib.AsyncExitStack() as stack:
        # Ensure SessionManager cleanup on exit
        stack.push_async_callback(session_manager.close_all)

        # Start the server
        read_stream, write_stream = await stack.enter_async_context(stdio_server())
        await server.run(read_stream, write_stream, options, raise_exceptions=True)
