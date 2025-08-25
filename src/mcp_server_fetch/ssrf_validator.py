import asyncio
import socket
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from typing import Any
from urllib.parse import urljoin, urlparse

from curl_cffi.requests import AsyncSession, Response
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData


class SSRFValidator:
    """Validates URLs to prevent Server-Side Request Forgery (SSRF) attacks.

    This validator blocks access to private IP ranges, localhost, and link-local
    addresses to prevent attackers from accessing internal services or cloud
    metadata endpoints.
    """

    BLOCKED_IPV4_NETWORKS: list[IPv4Network] = [
        IPv4Network("10.0.0.0/8"),  # Private network
        IPv4Network("172.16.0.0/12"),  # Private network
        IPv4Network("192.168.0.0/16"),  # Private network
        IPv4Network("127.0.0.0/8"),  # Loopback
        IPv4Network("169.254.0.0/16"),  # Link-local (AWS metadata, etc.)
        IPv4Network("0.0.0.0/8"),  # "This" network
        IPv4Network("224.0.0.0/4"),  # Multicast
        IPv4Network("240.0.0.0/4"),  # Reserved
        IPv4Network("100.64.0.0/10"),  # RFC 6598 Carrier-grade NAT
        IPv4Network("198.18.0.0/15"),  # RFC 2544 Network interconnect testing
        IPv4Network("203.0.113.0/24"),  # RFC 5737 Documentation
    ]

    BLOCKED_IPV6_NETWORKS: list[IPv6Network] = [
        IPv6Network("::1/128"),  # Loopback
        IPv6Network("fe80::/10"),  # Link-local
        IPv6Network("fc00::/7"),  # Unique local address
        IPv6Network("ff00::/8"),  # Multicast
        IPv6Network("::/128"),  # Unspecified address
        IPv6Network("::ffff:0:0/96"),  # IPv4-mapped IPv6 addresses
        IPv6Network("2001:db8::/32"),  # RFC 3849 Documentation
    ]

    DNS_TIMEOUT: float = 3.0  # DNS resolution timeout in seconds
    MAX_REDIRECTS: int = 10  # Maximum redirects to follow

    async def validate_url(self, url: str) -> None:
        """Validate a URL to prevent SSRF attacks.

        :param url: URL to validate
        :raises McpError: If the URL is blocked or invalid
        """
        parsed = urlparse(url)

        if not parsed.scheme or not parsed.netloc:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Invalid URL format"))

        if parsed.scheme not in ("http", "https"):
            raise McpError(
                ErrorData(
                    code=INVALID_PARAMS, message="Only HTTP and HTTPS URLs are allowed"
                )
            )

        # Extract hostname from netloc (remove port if present)
        hostname = parsed.hostname
        if not hostname:
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message="URL must contain a hostname")
            )

        # Check if hostname is already an IP address
        try:
            ip = ip_address(hostname)
            if self._is_blocked_ip(ip):
                raise McpError(
                    ErrorData(
                        code=INVALID_PARAMS,
                        message="Access to private or reserved IP ranges is not allowed",
                    )
                )
            return
        except ValueError:
            # Not an IP address, continue with DNS resolution
            pass

        # Resolve hostname and validate all resolved IPs
        await self._resolve_and_validate_hostname(hostname)

    async def _resolve_and_validate_hostname(self, hostname: str) -> None:
        """Resolve hostname and validate all resolved IP addresses.

        :param hostname: Hostname to resolve and validate
        :raises McpError: If resolution fails or any IP is blocked
        """
        try:
            # Async DNS resolution with timeout
            loop = asyncio.get_event_loop()
            addrinfo = await asyncio.wait_for(
                loop.getaddrinfo(
                    hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
                ),
                timeout=self.DNS_TIMEOUT,
            )
        except (socket.gaierror, asyncio.TimeoutError, OSError):
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message="Failed to resolve hostname")
            )

        if not addrinfo:
            raise McpError(
                ErrorData(
                    code=INVALID_PARAMS, message="No IP addresses resolved for hostname"
                )
            )

        # Check all resolved IP addresses
        for _, _, _, _, sockaddr in addrinfo:
            ip_str = sockaddr[0]
            try:
                ip = ip_address(ip_str)
                if self._is_blocked_ip(ip):
                    raise McpError(
                        ErrorData(
                            code=INVALID_PARAMS,
                            message="Access to private or reserved IP ranges is not allowed",
                        )
                    )
            except ValueError:
                # Should not happen with properly resolved addresses
                raise McpError(
                    ErrorData(
                        code=INVALID_PARAMS, message="Invalid IP address resolved"
                    )
                )

    async def follow_redirects_safely(
        self, session: AsyncSession[Response], initial_url: str, **kwargs: Any
    ) -> Response:
        """Follow redirects manually while validating each destination URL.

        :param session: AsyncSession to use for requests
        :param initial_url: Initial URL to fetch
        :param kwargs: Additional arguments to pass to session.get()
        :returns: Final Response object after following redirects
        :raises McpError: If any redirect destination is blocked or max redirects exceeded
        """
        current_url = initial_url
        redirect_count = 0

        # Validate the initial URL
        await self.validate_url(current_url)

        while redirect_count <= self.MAX_REDIRECTS:
            # Make request with redirects disabled
            response = await session.get(current_url, allow_redirects=False, **kwargs)

            # If not a redirect, return the response
            if response.status_code not in (301, 302, 303, 307, 308):
                return response

            # Get redirect location
            location = response.headers.get("location")
            if not location:
                # Redirect response without location header
                return response

            # Handle relative redirects
            next_url = urljoin(current_url, location)

            # Validate the redirect destination
            await self.validate_url(next_url)

            current_url = next_url
            redirect_count += 1

        # Too many redirects
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Too many redirects"))

    def _is_blocked_ip(self, ip: IPv4Address | IPv6Address) -> bool:
        """Check if an IP address is in a blocked range.

        :param ip: IP address to check
        :returns: True if the IP is blocked, False otherwise
        """
        # Check IPv4-mapped IPv6 addresses against IPv4 rules first
        if isinstance(ip, IPv6Address) and ip.ipv4_mapped:
            return any(
                ip.ipv4_mapped in network for network in self.BLOCKED_IPV4_NETWORKS
            )
        elif isinstance(ip, IPv4Address):
            return any(ip in network for network in self.BLOCKED_IPV4_NETWORKS)
        else:  # Pure IPv6
            return any(ip in network for network in self.BLOCKED_IPV6_NETWORKS)
