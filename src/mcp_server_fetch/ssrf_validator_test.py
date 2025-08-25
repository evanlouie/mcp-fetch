import asyncio
import socket
from ipaddress import IPv4Address, IPv6Address
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData

from .ssrf_validator import SSRFValidator


class TestSSRFValidator:
    """Test suite for SSRFValidator class."""

    validator: SSRFValidator

    def setup_method(self):
        """Setup validator instance for each test."""
        self.validator = SSRFValidator()

    @pytest.mark.asyncio
    async def test_validate_url_valid_http(self):
        """Test validation of valid HTTP URLs."""
        with patch.object(
            self.validator, "_resolve_and_validate_hostname", new_callable=AsyncMock
        ):
            await self.validator.validate_url("http://example.com")
            await self.validator.validate_url("https://google.com")

    @pytest.mark.asyncio
    async def test_validate_url_invalid_format(self):
        """Test validation rejects invalid URL formats."""
        with pytest.raises(McpError, match="Invalid URL format"):
            await self.validator.validate_url("")

        with pytest.raises(McpError, match="Invalid URL format"):
            await self.validator.validate_url("not-a-url")

        with pytest.raises(McpError, match="Invalid URL format"):
            await self.validator.validate_url("://missing-scheme")

    @pytest.mark.asyncio
    async def test_validate_url_invalid_scheme(self):
        """Test validation rejects non-HTTP schemes."""
        with pytest.raises(McpError, match="Only HTTP and HTTPS URLs are allowed"):
            await self.validator.validate_url("ftp://example.com")

        # file:/// URLs don't have netloc so they're caught as "Invalid URL format"
        with pytest.raises(McpError, match="Invalid URL format"):
            await self.validator.validate_url("file:///etc/passwd")

        with pytest.raises(McpError, match="Only HTTP and HTTPS URLs are allowed"):
            await self.validator.validate_url("gopher://example.com")

    @pytest.mark.asyncio
    async def test_validate_url_missing_hostname(self):
        """Test validation rejects URLs without hostname."""
        # URLs without netloc are caught as "Invalid URL format"
        with pytest.raises(McpError, match="Invalid URL format"):
            await self.validator.validate_url("http://")

    @pytest.mark.asyncio
    async def test_validate_url_localhost_ipv4(self):
        """Test blocking of localhost IPv4 addresses."""
        with pytest.raises(
            McpError, match="Access to private or reserved IP ranges is not allowed"
        ):
            await self.validator.validate_url("http://127.0.0.1")

        with pytest.raises(
            McpError, match="Access to private or reserved IP ranges is not allowed"
        ):
            await self.validator.validate_url("http://127.0.0.1:8080")

        with pytest.raises(
            McpError, match="Access to private or reserved IP ranges is not allowed"
        ):
            await self.validator.validate_url("https://127.1.1.1")

    @pytest.mark.asyncio
    async def test_validate_url_private_ipv4(self):
        """Test blocking of private IPv4 ranges."""
        private_ips = [
            "10.0.0.1",  # 10.0.0.0/8
            "172.16.0.1",  # 172.16.0.0/12
            "172.31.255.255",  # 172.16.0.0/12 upper bound
            "192.168.1.1",  # 192.168.0.0/16
            "192.168.255.255",  # 192.168.0.0/16 upper bound
        ]

        for ip in private_ips:
            with pytest.raises(
                McpError, match="Access to private or reserved IP ranges is not allowed"
            ):
                await self.validator.validate_url(f"http://{ip}")

    @pytest.mark.asyncio
    async def test_validate_url_link_local_ipv4(self):
        """Test blocking of link-local addresses (AWS metadata, etc.)."""
        with pytest.raises(
            McpError, match="Access to private or reserved IP ranges is not allowed"
        ):
            await self.validator.validate_url("http://169.254.169.254")

        with pytest.raises(
            McpError, match="Access to private or reserved IP ranges is not allowed"
        ):
            await self.validator.validate_url("http://169.254.169.254/metadata")

    @pytest.mark.asyncio
    async def test_validate_url_reserved_ipv4(self):
        """Test blocking of reserved IPv4 ranges."""
        reserved_ips = [
            "0.0.0.0",  # "This" network
            "224.0.0.1",  # Multicast
            "240.0.0.1",  # Reserved
        ]

        for ip in reserved_ips:
            with pytest.raises(
                McpError, match="Access to private or reserved IP ranges is not allowed"
            ):
                await self.validator.validate_url(f"http://{ip}")

    @pytest.mark.asyncio
    async def test_validate_url_localhost_ipv6(self):
        """Test blocking of localhost IPv6 addresses."""
        with pytest.raises(
            McpError, match="Access to private or reserved IP ranges is not allowed"
        ):
            await self.validator.validate_url("http://[::1]")

        with pytest.raises(
            McpError, match="Access to private or reserved IP ranges is not allowed"
        ):
            await self.validator.validate_url("http://[::1]:8080")

    @pytest.mark.asyncio
    async def test_validate_url_private_ipv6(self):
        """Test blocking of private IPv6 ranges."""
        private_ipv6s = [
            "fe80::1",  # Link-local
            "fc00::1",  # Unique local address
            "ff00::1",  # Multicast
            "::",  # Unspecified address
        ]

        for ip in private_ipv6s:
            with pytest.raises(
                McpError, match="Access to private or reserved IP ranges is not allowed"
            ):
                await self.validator.validate_url(f"http://[{ip}]")

    @pytest.mark.asyncio
    async def test_validate_url_public_ipv4_allowed(self):
        """Test that public IPv4 addresses are allowed."""
        public_ips = [
            "8.8.8.8",  # Google DNS
            "1.1.1.1",  # Cloudflare DNS
            "208.67.222.222",  # OpenDNS
        ]

        for ip in public_ips:
            # Should not raise an exception
            await self.validator.validate_url(f"http://{ip}")

    @pytest.mark.asyncio
    async def test_validate_url_public_ipv6_allowed(self):
        """Test that public IPv6 addresses are allowed."""
        public_ipv6s = [
            "2001:4860:4860::8888",  # Google DNS
            "2606:4700:4700::1111",  # Cloudflare DNS
        ]

        for ip in public_ipv6s:
            # Should not raise an exception
            await self.validator.validate_url(f"http://[{ip}]")

    @pytest.mark.asyncio
    async def test_resolve_and_validate_hostname_blocked_resolution(self):
        """Test hostname that resolves to blocked IP addresses."""
        # Mock DNS resolution to return localhost
        mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

        with patch("asyncio.get_event_loop") as mock_loop:
            mock_event_loop = AsyncMock()
            mock_loop.return_value = mock_event_loop
            mock_event_loop.getaddrinfo = AsyncMock(return_value=mock_addrinfo)

            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
                mock_wait_for.return_value = mock_addrinfo

                with pytest.raises(
                    McpError,
                    match="Access to private or reserved IP ranges is not allowed",
                ):
                    await self.validator._resolve_and_validate_hostname("evil.com")

    @pytest.mark.asyncio
    async def test_resolve_and_validate_hostname_dns_timeout(self):
        """Test DNS timeout handling."""
        with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
            mock_wait_for.side_effect = asyncio.TimeoutError()

            with pytest.raises(McpError, match="Failed to resolve hostname"):
                await self.validator._resolve_and_validate_hostname("timeout.com")

    @pytest.mark.asyncio
    async def test_resolve_and_validate_hostname_dns_error(self):
        """Test DNS resolution error handling."""
        with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
            mock_wait_for.side_effect = socket.gaierror("DNS resolution failed")

            with pytest.raises(McpError, match="Failed to resolve hostname"):
                await self.validator._resolve_and_validate_hostname("nonexistent.com")

    @pytest.mark.asyncio
    async def test_resolve_and_validate_hostname_no_results(self):
        """Test handling of empty DNS resolution results."""
        with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
            mock_wait_for.return_value = []

            with pytest.raises(McpError, match="No IP addresses resolved for hostname"):
                await self.validator._resolve_and_validate_hostname("empty.com")

    @pytest.mark.asyncio
    async def test_resolve_and_validate_hostname_valid(self):
        """Test successful DNS resolution to public IP."""
        # Mock DNS resolution to return public IP
        mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]

        with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
            mock_wait_for.return_value = mock_addrinfo

            # Should not raise an exception
            await self.validator._resolve_and_validate_hostname("google.com")

    @pytest.mark.asyncio
    async def test_follow_redirects_safely_no_redirects(self):
        """Test handling of non-redirect responses."""
        mock_session = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_session.get.return_value = mock_response

        with patch.object(
            self.validator, "validate_url", new_callable=AsyncMock
        ) as mock_validate:
            result = await self.validator.follow_redirects_safely(
                mock_session, "http://example.com"
            )

            assert result == mock_response
            mock_validate.assert_called_once_with("http://example.com")
            mock_session.get.assert_called_once_with(
                "http://example.com", allow_redirects=False
            )

    @pytest.mark.asyncio
    async def test_follow_redirects_safely_valid_redirect(self):
        """Test following valid redirects."""
        mock_session = AsyncMock()

        # First response is a redirect
        mock_redirect_response = MagicMock()
        mock_redirect_response.status_code = 301
        mock_redirect_response.headers = {"location": "https://example.com/new"}

        # Second response is final
        mock_final_response = MagicMock()
        mock_final_response.status_code = 200

        mock_session.get.side_effect = [mock_redirect_response, mock_final_response]

        with patch.object(
            self.validator, "validate_url", new_callable=AsyncMock
        ) as mock_validate:
            result = await self.validator.follow_redirects_safely(
                mock_session, "http://example.com"
            )

            assert result == mock_final_response
            assert mock_validate.call_count == 2
            mock_validate.assert_any_call("http://example.com")
            mock_validate.assert_any_call("https://example.com/new")

    @pytest.mark.asyncio
    async def test_follow_redirects_safely_blocked_redirect(self):
        """Test blocking redirects to malicious destinations."""
        mock_session = AsyncMock()

        mock_redirect_response = MagicMock()
        mock_redirect_response.status_code = 302
        mock_redirect_response.headers = {"location": "http://localhost:8080"}

        mock_session.get.return_value = mock_redirect_response

        with patch.object(
            self.validator, "validate_url", new_callable=AsyncMock
        ) as mock_validate:
            # First validation passes (initial URL)
            # Second validation fails (redirect destination)
            mock_validate.side_effect = [
                None,
                McpError(ErrorData(code=INVALID_PARAMS, message="Blocked URL")),
            ]

            with pytest.raises(McpError):
                await self.validator.follow_redirects_safely(
                    mock_session, "http://example.com"
                )

    @pytest.mark.asyncio
    async def test_follow_redirects_safely_relative_redirect(self):
        """Test handling of relative redirects."""
        mock_session = AsyncMock()

        mock_redirect_response = MagicMock()
        mock_redirect_response.status_code = 301
        mock_redirect_response.headers = {"location": "/new-path"}

        mock_final_response = MagicMock()
        mock_final_response.status_code = 200

        mock_session.get.side_effect = [mock_redirect_response, mock_final_response]

        with patch.object(
            self.validator, "validate_url", new_callable=AsyncMock
        ) as mock_validate:
            await self.validator.follow_redirects_safely(
                mock_session, "http://example.com/old-path"
            )

            # Should validate the resolved absolute URL
            mock_validate.assert_any_call("http://example.com/new-path")

    @pytest.mark.asyncio
    async def test_follow_redirects_safely_too_many_redirects(self):
        """Test handling of too many redirects."""
        mock_session = AsyncMock()

        mock_redirect_response = MagicMock()
        mock_redirect_response.status_code = 301
        mock_redirect_response.headers = {"location": "http://example.com/loop"}

        mock_session.get.return_value = mock_redirect_response

        with patch.object(self.validator, "validate_url", new_callable=AsyncMock):
            with pytest.raises(McpError, match="Too many redirects"):
                await self.validator.follow_redirects_safely(
                    mock_session, "http://example.com"
                )

    @pytest.mark.asyncio
    async def test_follow_redirects_safely_redirect_without_location(self):
        """Test handling of redirect response without location header."""
        mock_session = AsyncMock()

        mock_redirect_response = MagicMock()
        mock_redirect_response.status_code = 301
        mock_redirect_response.headers = {}

        mock_session.get.return_value = mock_redirect_response

        with patch.object(
            self.validator, "validate_url", new_callable=AsyncMock
        ) as mock_validate:
            result = await self.validator.follow_redirects_safely(
                mock_session, "http://example.com"
            )

            # Should return the redirect response without location
            assert result == mock_redirect_response
            mock_validate.assert_called_once_with("http://example.com")

    def test_is_blocked_ip_ipv4_private(self):
        """Test IP blocking for IPv4 private ranges."""
        blocked_ips = [
            IPv4Address("10.0.0.1"),
            IPv4Address("172.16.0.1"),
            IPv4Address("192.168.1.1"),
            IPv4Address("127.0.0.1"),
            IPv4Address("169.254.169.254"),
            IPv4Address("0.0.0.0"),
            IPv4Address("224.0.0.1"),
            IPv4Address("240.0.0.1"),
        ]

        for ip in blocked_ips:
            assert self.validator._is_blocked_ip(ip), f"Should block {ip}"

    def test_is_blocked_ip_ipv4_public(self):
        """Test IP blocking allows IPv4 public addresses."""
        public_ips = [
            IPv4Address("8.8.8.8"),
            IPv4Address("1.1.1.1"),
            IPv4Address("208.67.222.222"),
        ]

        for ip in public_ips:
            assert not self.validator._is_blocked_ip(ip), f"Should allow {ip}"

    def test_is_blocked_ip_ipv6_private(self):
        """Test IP blocking for IPv6 private ranges."""
        blocked_ips = [
            IPv6Address("::1"),
            IPv6Address("fe80::1"),
            IPv6Address("fc00::1"),
            IPv6Address("ff00::1"),
            IPv6Address("::"),
        ]

        for ip in blocked_ips:
            assert self.validator._is_blocked_ip(ip), f"Should block {ip}"

    def test_is_blocked_ip_ipv6_public(self):
        """Test IP blocking allows IPv6 public addresses."""
        public_ips = [
            IPv6Address("2001:4860:4860::8888"),
            IPv6Address("2606:4700:4700::1111"),
        ]

        for ip in public_ips:
            assert not self.validator._is_blocked_ip(ip), f"Should allow {ip}"

    @pytest.mark.asyncio
    async def test_bypass_prevention_decimal_encoding(self):
        """Test prevention of decimal IP encoding bypass attempts."""
        # 127.0.0.1 in decimal is 2130706433
        with pytest.raises(
            McpError, match="Access to private or reserved IP ranges is not allowed"
        ):
            await self.validator.validate_url("http://2130706433")

    @pytest.mark.asyncio
    async def test_comprehensive_security_validation(self):
        """Test comprehensive security validation scenarios."""
        # Test various attack vectors that should be blocked
        attack_urls = [
            "http://localhost",
            "http://127.0.0.1",
            "http://127.1",  # Alternative localhost representation
            "http://0.0.0.0",
            "http://10.0.0.1",
            "http://172.16.0.1",
            "http://192.168.1.1",
            "http://169.254.169.254",  # AWS metadata
            "http://[::1]",  # IPv6 localhost
            "http://[fe80::1]",  # IPv6 link-local
            "http://[fc00::1]",  # IPv6 unique local
        ]

        for url in attack_urls:
            with pytest.raises(
                McpError, match="Access to private or reserved IP ranges is not allowed"
            ):
                await self.validator.validate_url(url)

    @pytest.mark.asyncio
    async def test_ipv4_mapped_ipv6_localhost_blocking(self):
        """Test blocking of IPv4-mapped IPv6 localhost addresses (critical vulnerability fix)."""
        ipv4_mapped_localhost = [
            "http://[::ffff:127.0.0.1]",  # Standard IPv4-mapped IPv6 localhost
            "http://[::ffff:127.0.0.1]:8080",  # With port
            "http://[::ffff:7f00:1]",  # 127.0.0.1 in hex format
        ]

        for url in ipv4_mapped_localhost:
            with pytest.raises(
                McpError, match="Access to private or reserved IP ranges is not allowed"
            ):
                await self.validator.validate_url(url)

    @pytest.mark.asyncio
    async def test_ipv4_mapped_ipv6_private_blocking(self):
        """Test blocking of IPv4-mapped IPv6 private network addresses."""
        ipv4_mapped_private = [
            "http://[::ffff:10.0.0.1]",  # 10.0.0.1 - private network
            "http://[::ffff:192.168.1.1]",  # 192.168.1.1 - private network
            "http://[::ffff:172.16.0.1]",  # 172.16.0.1 - private network
            "http://[::ffff:169.254.169.254]",  # AWS metadata service
        ]

        for url in ipv4_mapped_private:
            with pytest.raises(
                McpError, match="Access to private or reserved IP ranges is not allowed"
            ):
                await self.validator.validate_url(url)

    @pytest.mark.asyncio
    async def test_missing_ipv4_ranges_blocking(self):
        """Test blocking of previously missing IPv4 ranges."""
        missing_ranges = [
            "http://100.64.0.1",  # Carrier-grade NAT
            "http://198.18.0.1",  # Network testing
            "http://203.0.113.1",  # Documentation
        ]

        for url in missing_ranges:
            with pytest.raises(
                McpError, match="Access to private or reserved IP ranges is not allowed"
            ):
                await self.validator.validate_url(url)

    @pytest.mark.asyncio
    async def test_missing_ipv6_ranges_blocking(self):
        """Test blocking of previously missing IPv6 ranges."""
        missing_ranges = [
            "http://[2001:db8::1]",  # IPv6 documentation range
        ]

        for url in missing_ranges:
            with pytest.raises(
                McpError, match="Access to private or reserved IP ranges is not allowed"
            ):
                await self.validator.validate_url(url)

    @pytest.mark.asyncio
    async def test_valid_urls_allowed(self):
        """Test that legitimate URLs are allowed through."""
        valid_urls = [
            "http://8.8.8.8",  # Public IPv4
            "https://1.1.1.1",  # Public IPv4 HTTPS
            "http://[2001:4860:4860::8888]",  # Public IPv6
        ]

        for url in valid_urls:
            # These should not raise exceptions
            await self.validator.validate_url(url)
