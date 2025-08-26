from unittest.mock import AsyncMock, Mock, patch

import pytest
from curl_cffi.requests.exceptions import RequestException
from mcp.shared.exceptions import McpError

from .server import (
    HttpConfig,
    HttpResponse,
    ProcessedContent,
    _execute_http_request,
    _process_response_content,
    _validate_http_response,
    fetch_url_legacy,
    fetch_url_pooled,
    process_content,
)


class TestHttpConfig:
    """Test HttpConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = HttpConfig()
        assert config.proxy_url is None
        assert config.timeout == 30
        assert "Mozilla/5.0" in config.user_agent

    def test_custom_values(self):
        """Test custom configuration values."""
        config = HttpConfig(
            proxy_url="http://proxy.example.com:8080",
            timeout=60,
            user_agent="Custom User Agent",
        )
        assert config.proxy_url == "http://proxy.example.com:8080"
        assert config.timeout == 60
        assert config.user_agent == "Custom User Agent"

    def test_validation_errors(self):
        """Test Pydantic validation errors."""
        with pytest.raises(Exception):  # ValidationError
            HttpConfig(timeout=-1)  # Invalid timeout

        with pytest.raises(Exception):  # ValidationError
            HttpConfig(timeout=1000)  # Timeout too large


class TestHttpResponse:
    """Test HttpResponse dataclass."""

    def test_creation(self):
        """Test HttpResponse creation."""
        response = HttpResponse(
            content="<html>Test</html>",
            status_code=200,
            headers={"content-type": "text/html"},
            url="https://example.com",
        )
        assert response.content == "<html>Test</html>"
        assert response.status_code == 200
        assert response.headers == {"content-type": "text/html"}
        assert response.url == "https://example.com"


class TestExecuteHttpRequest:
    """Test _execute_http_request function."""

    @pytest.mark.asyncio
    async def test_successful_request(self):
        """Test successful HTTP request execution."""
        # Mock session and response
        mock_session = AsyncMock()
        mock_response = Mock()
        mock_response.text = "<html>Test content</html>"
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html", "server": "nginx"}
        mock_response.url = "https://example.com"

        # Mock SSRF validator
        mock_ssrf_validator = Mock()
        mock_ssrf_validator.follow_redirects_safely = AsyncMock(
            return_value=mock_response
        )

        config = HttpConfig(timeout=30)

        result = await _execute_http_request(
            "https://example.com", mock_session, config, mock_ssrf_validator
        )

        assert isinstance(result, HttpResponse)
        assert result.content == "<html>Test content</html>"
        assert result.status_code == 200
        assert result.headers == {"content-type": "text/html", "server": "nginx"}
        assert result.url == "https://example.com"

        # Verify SSRF validator was called correctly
        mock_ssrf_validator.follow_redirects_safely.assert_called_once_with(
            mock_session,
            "https://example.com",
            headers={"User-Agent": config.user_agent},
            timeout=30,
            proxy=None,
        )

    @pytest.mark.asyncio
    async def test_request_with_proxy(self):
        """Test HTTP request with proxy configuration."""
        mock_session = AsyncMock()
        mock_response = Mock()
        mock_response.text = "Content"
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.url = "https://example.com"

        mock_ssrf_validator = Mock()
        mock_ssrf_validator.follow_redirects_safely = AsyncMock(
            return_value=mock_response
        )

        config = HttpConfig(proxy_url="http://proxy.example.com:8080")

        await _execute_http_request(
            "https://example.com", mock_session, config, mock_ssrf_validator
        )

        # Verify proxy was passed correctly
        mock_ssrf_validator.follow_redirects_safely.assert_called_once_with(
            mock_session,
            "https://example.com",
            headers={"User-Agent": config.user_agent},
            timeout=30,
            proxy="http://proxy.example.com:8080",
        )

    @pytest.mark.asyncio
    async def test_request_exception(self):
        """Test handling of request exceptions."""
        mock_session = AsyncMock()
        mock_ssrf_validator = Mock()
        mock_ssrf_validator.follow_redirects_safely = AsyncMock(
            side_effect=RequestException("Connection failed")
        )

        config = HttpConfig()

        with pytest.raises(RequestException, match="Connection failed"):
            await _execute_http_request(
                "https://example.com", mock_session, config, mock_ssrf_validator
            )

    @pytest.mark.asyncio
    async def test_headers_with_none_values(self):
        """Test handling of headers with None values."""
        mock_session = AsyncMock()
        mock_response = Mock()
        mock_response.text = "Content"
        mock_response.status_code = 200
        mock_response.headers = {
            "content-type": "text/html",
            "server": None,
            "cache-control": "no-cache",
        }
        mock_response.url = "https://example.com"

        mock_ssrf_validator = Mock()
        mock_ssrf_validator.follow_redirects_safely = AsyncMock(
            return_value=mock_response
        )

        config = HttpConfig()

        result = await _execute_http_request(
            "https://example.com", mock_session, config, mock_ssrf_validator
        )

        # None values should be filtered out
        assert result.headers == {
            "content-type": "text/html",
            "cache-control": "no-cache",
        }


class TestValidateHttpResponse:
    """Test _validate_http_response function."""

    def test_valid_response(self):
        """Test validation of successful response."""
        response = HttpResponse(
            content="content", status_code=200, headers={}, url="https://example.com"
        )
        # Should not raise any exception
        _validate_http_response(response)

    def test_client_error_response(self):
        """Test validation of 4xx client error."""
        response = HttpResponse(
            content="Not Found", status_code=404, headers={}, url="https://example.com"
        )

        with pytest.raises(McpError) as exc_info:
            _validate_http_response(response)

        assert "Client error" in str(exc_info.value)
        assert "HTTP 404" in str(exc_info.value)
        assert "https://example.com" in str(exc_info.value)

    def test_server_error_response(self):
        """Test validation of 5xx server error."""
        response = HttpResponse(
            content="Internal Server Error",
            status_code=500,
            headers={},
            url="https://example.com",
        )

        with pytest.raises(McpError) as exc_info:
            _validate_http_response(response)

        assert "Server error" in str(exc_info.value)
        assert "HTTP 500" in str(exc_info.value)

    def test_redirect_response(self):
        """Test validation of 3xx redirect (should pass)."""
        response = HttpResponse(
            content="Moved", status_code=301, headers={}, url="https://example.com"
        )
        # Should not raise any exception since redirects are handled by SSRF validator
        _validate_http_response(response)


class TestProcessResponseContent:
    """Test _process_response_content function."""

    def test_html_content_processed(self):
        """Test HTML content is processed to markdown."""
        html_content = "<html><body><h1>Title</h1><p>Content</p></body></html>"
        response = HttpResponse(
            content=html_content,
            status_code=200,
            headers={"content-type": "text/html"},
            url="https://example.com",
        )

        result = _process_response_content(response, force_raw=False)

        # Should be processed by extract_content_from_html
        assert isinstance(result, ProcessedContent)
        assert result.content != html_content
        assert result.prefix == ""

    def test_html_content_force_raw(self):
        """Test HTML content returned raw when force_raw=True."""
        html_content = "<html><body><h1>Title</h1><p>Content</p></body></html>"
        response = HttpResponse(
            content=html_content,
            status_code=200,
            headers={"content-type": "text/html"},
            url="https://example.com",
        )

        result = _process_response_content(response, force_raw=True)

        assert isinstance(result, ProcessedContent)
        assert result.content == html_content
        assert "text/html" in result.prefix
        assert "raw content" in result.prefix

    def test_xhtml_content(self):
        """Test XHTML content is processed as HTML."""
        xhtml_content = '<?xml version="1.0"?><html><body><p>Content</p></body></html>'
        response = HttpResponse(
            content=xhtml_content,
            status_code=200,
            headers={"content-type": "application/xhtml+xml"},
            url="https://example.com",
        )

        result = _process_response_content(response, force_raw=False)

        assert isinstance(result, ProcessedContent)
        assert result.content != xhtml_content
        assert result.prefix == ""

    def test_html_detection_by_content(self):
        """Test HTML detection by content when content-type is missing."""
        html_content = "<!DOCTYPE html><html><body><p>Content</p></body></html>"
        response = HttpResponse(
            content=html_content,
            status_code=200,
            headers={},  # No content-type header
            url="https://example.com",
        )

        result = _process_response_content(response, force_raw=False)

        assert isinstance(result, ProcessedContent)
        assert result.content != html_content
        assert result.prefix == ""

    def test_plain_text_content(self):
        """Test plain text content is returned as-is."""
        text_content = "This is plain text content."
        response = HttpResponse(
            content=text_content,
            status_code=200,
            headers={"content-type": "text/plain"},
            url="https://example.com",
        )

        result = _process_response_content(response, force_raw=False)

        assert isinstance(result, ProcessedContent)
        assert result.content == text_content
        assert "text/plain" in result.prefix
        assert "raw content" in result.prefix

    def test_json_content(self):
        """Test JSON content is returned as-is with appropriate prefix."""
        json_content = '{"key": "value", "number": 123}'
        response = HttpResponse(
            content=json_content,
            status_code=200,
            headers={"content-type": "application/json"},
            url="https://example.com",
        )

        result = _process_response_content(response, force_raw=False)

        assert isinstance(result, ProcessedContent)
        assert result.content == json_content
        assert "application/json" in result.prefix
        assert "raw content" in result.prefix

    def test_no_content_type_no_html_markers(self):
        """Test content without content-type or HTML markers."""
        plain_content = "Just some text without HTML markers."
        response = HttpResponse(
            content=plain_content,
            status_code=200,
            headers={},
            url="https://example.com",
        )

        result = _process_response_content(response, force_raw=False)

        assert isinstance(result, ProcessedContent)
        assert result.content == plain_content
        assert "raw content" in result.prefix

    def test_case_insensitive_content_type(self):
        """Test case-insensitive content type detection."""
        html_content = "<html><body><p>Content</p></body></html>"
        response = HttpResponse(
            content=html_content,
            status_code=200,
            headers={"content-type": "TEXT/HTML; charset=utf-8"},
            url="https://example.com",
        )

        result = _process_response_content(response, force_raw=False)

        assert isinstance(result, ProcessedContent)
        assert result.content != html_content
        assert result.prefix == ""


class TestIntegration:
    """Integration tests for the updated main functions."""

    @pytest.mark.asyncio
    async def test_fetch_url_legacy_success(self):
        """Test fetch_url_legacy with mocked dependencies."""

        # Mock the helper functions
        mock_http_response = HttpResponse(
            content="<html><body><h1>Test</h1></body></html>",
            status_code=200,
            headers={"content-type": "text/html"},
            url="https://example.com",
        )

        with (
            patch(
                "mcp_server_fetch.server._execute_http_request", new_callable=AsyncMock
            ) as mock_execute,
            patch("mcp_server_fetch.server._validate_http_response") as mock_validate,
            patch("mcp_server_fetch.server._process_response_content") as mock_process,
        ):
            mock_execute.return_value = mock_http_response
            mock_validate.return_value = None
            mock_process.return_value = ProcessedContent(
                content="# Test\n\nProcessed content", prefix=""
            )

            content, prefix = await fetch_url_legacy(
                "https://example.com", force_raw=False
            )

            assert content == "# Test\n\nProcessed content"
            assert prefix == ""

            # Verify the pipeline was called correctly
            mock_execute.assert_called_once()
            mock_validate.assert_called_once_with(mock_http_response)
            mock_process.assert_called_once_with(mock_http_response, False)

    @pytest.mark.asyncio
    async def test_fetch_url_pooled_success(self):
        """Test fetch_url_pooled with mocked dependencies."""

        # Mock session manager
        mock_session_manager = AsyncMock()
        mock_session = AsyncMock()
        mock_session_manager.get_session.return_value = mock_session
        mock_session_manager.handle_request_error.return_value = False

        mock_http_response = HttpResponse(
            content="Plain text content",
            status_code=200,
            headers={"content-type": "text/plain"},
            url="https://example.com",
        )

        with (
            patch(
                "mcp_server_fetch.server._execute_http_request", new_callable=AsyncMock
            ) as mock_execute,
            patch("mcp_server_fetch.server._validate_http_response") as mock_validate,
            patch("mcp_server_fetch.server._process_response_content") as mock_process,
        ):
            mock_execute.return_value = mock_http_response
            mock_validate.return_value = None
            mock_process.return_value = ProcessedContent(
                content="Plain text content",
                prefix="Content type text/plain cannot be simplified to markdown, but here is the raw content:\n",
            )

            content, prefix = await fetch_url_pooled(
                "https://example.com",
                mock_session_manager,
                force_raw=False,
                proxy_url="http://proxy.example.com:8080",
            )

            assert content == "Plain text content"
            assert "text/plain" in prefix

            # Verify session manager was used
            mock_session_manager.get_session.assert_called_once()
            mock_execute.assert_called_once()
            mock_validate.assert_called_once_with(mock_http_response)
            mock_process.assert_called_once_with(mock_http_response, False)

    @pytest.mark.asyncio
    async def test_fetch_url_pooled_with_retry(self):
        """Test fetch_url_pooled retry logic on request failure."""

        # Mock session manager
        mock_session_manager = AsyncMock()
        mock_session = AsyncMock()
        mock_session_manager.get_session.return_value = mock_session
        mock_session_manager.handle_request_error.return_value = (
            True  # Should recreate session
        )

        mock_http_response = HttpResponse(
            content="Success after retry",
            status_code=200,
            headers={"content-type": "text/html"},
            url="https://example.com",
        )

        with (
            patch(
                "mcp_server_fetch.server._execute_http_request", new_callable=AsyncMock
            ) as mock_execute,
            patch("mcp_server_fetch.server._validate_http_response") as mock_validate,
            patch("mcp_server_fetch.server._process_response_content") as mock_process,
        ):
            # First call fails, second succeeds
            mock_execute.side_effect = [
                RequestException("Connection failed"),
                mock_http_response,
            ]
            mock_validate.return_value = None
            mock_process.return_value = ProcessedContent(
                content="Success after retry processed", prefix=""
            )

            content, prefix = await fetch_url_pooled(
                "https://example.com", mock_session_manager, force_raw=False
            )

            assert content == "Success after retry processed"
            assert prefix == ""

            # Verify retry logic
            assert mock_execute.call_count == 2
            assert mock_session_manager.get_session.call_count == 2
            mock_session_manager.handle_request_error.assert_called_once()

    def test_process_content_legacy_wrapper(self):
        """Test the backward compatibility wrapper for process_content."""

        # Mock curl_cffi Response object
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {
            "content-type": "text/html",
            "server": "nginx",
            "cache-control": None,  # Test None value filtering
        }
        mock_response.url = "https://example.com"

        page_raw = "<html><body><h1>Test</h1></body></html>"

        content, prefix = process_content(page_raw, mock_response, force_raw=True)

        # Should return raw content when force_raw=True
        assert content == page_raw
        assert "text/html" in prefix
        assert "raw content" in prefix

    @pytest.mark.asyncio
    async def test_error_classification_integration(self):
        """Test that error classification works end-to-end."""

        mock_http_response_client_error = HttpResponse(
            content="Not Found", status_code=404, headers={}, url="https://example.com"
        )

        mock_http_response_server_error = HttpResponse(
            content="Internal Server Error",
            status_code=500,
            headers={},
            url="https://example.com",
        )

        with patch(
            "mcp_server_fetch.server._execute_http_request", new_callable=AsyncMock
        ) as mock_execute:
            # Test client error (4xx)
            mock_execute.return_value = mock_http_response_client_error
            with pytest.raises(McpError) as exc_info:
                await fetch_url_legacy("https://example.com")
            assert "Client error" in str(exc_info.value)
            assert "HTTP 404" in str(exc_info.value)

            # Test server error (5xx)
            mock_execute.return_value = mock_http_response_server_error
            with pytest.raises(McpError) as exc_info:
                await fetch_url_legacy("https://example.com")
            assert "Server error" in str(exc_info.value)
            assert "HTTP 500" in str(exc_info.value)
