import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests.exceptions import RequestException


@dataclass(frozen=True)
class SessionConfig:
    """Configuration for AsyncSession creation.

    This dataclass serves as a hashable key for session caching,
    ensuring different configurations get separate session instances.
    """

    impersonate: str = "chrome"
    proxy_url: str | None = None
    max_connections: int = 25
    max_connections_per_host: int = 6
    keep_alive_timeout: float = 60.0
    verify_ssl: bool = True


SESSION_MAX_AGE = timedelta(hours=1)
SESSION_IDLE_GRACE_PERIOD = timedelta(minutes=5)
SESSION_MAX_ERRORS = 3


@dataclass
class SessionHealth:
    """Tracks session health metrics for automatic recreation."""

    last_used: datetime = field(default_factory=datetime.now)
    error_count: int = 0
    request_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)

    def is_healthy(self) -> bool:
        """Check if session is healthy based on error rate and age."""
        now = datetime.now()

        if self.error_count >= SESSION_MAX_ERRORS:
            return False

        age = now - self.created_at
        if age < SESSION_MAX_AGE:
            return True

        # Allow recently-used sessions to stay alive even if they are old.
        idle_time = now - self.last_used
        return idle_time < SESSION_IDLE_GRACE_PERIOD

    def record_success(self) -> None:
        """Record successful request."""
        self.last_used = datetime.now()
        self.request_count += 1

    def record_error(self) -> None:
        """Record request error."""
        self.last_used = datetime.now()
        self.error_count += 1


class SessionManagerError(Exception):
    """Base exception for SessionManager operations."""

    pass


class SessionManager:
    """Manages shared AsyncSession instances with connection pooling.

    Provides optimized session management for MCP server usage patterns:
    - Lazy initialization of sessions per configuration
    - Per-host locking to prevent contention bottlenecks
    - Health monitoring and automatic session recreation
    - Connection pool optimization for AI workload bursts
    - Graceful error handling with fallback mechanisms
    """

    def __init__(self) -> None:
        self._sessions: dict[SessionConfig, AsyncSession[Response]] = {}
        self._session_health: dict[SessionConfig, SessionHealth] = {}
        self._locks: defaultdict[SessionConfig, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )
        self._cleanup_task: asyncio.Task[None] | None = None

    def _start_cleanup_task(self) -> None:
        """Start background task for session health monitoring."""
        try:
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        except RuntimeError:
            # No event loop running, cleanup task will be started when first session is requested
            pass

    async def _periodic_cleanup(self) -> None:
        """Periodically clean up unhealthy sessions."""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self._cleanup_unhealthy_sessions()
            except asyncio.CancelledError:
                break
            except Exception:
                # Continue cleanup even if individual cleanup fails
                pass

    async def _cleanup_unhealthy_sessions(self) -> None:
        """Remove and close unhealthy sessions."""
        unhealthy_configs = []

        for config, health in self._session_health.items():
            if not health.is_healthy():
                unhealthy_configs.append(config)

        for config in unhealthy_configs:
            await self._close_session(config)

    async def _close_session(self, config: SessionConfig) -> None:
        """Close and remove a specific session."""
        session = self._sessions.pop(config, None)
        if session:
            try:
                await session.close()
            except Exception:
                pass  # Ignore errors during cleanup

        self._session_health.pop(config, None)

        # Clean up the lock to prevent memory leaks
        _ = self._locks.pop(config, None)

    async def get_session(self, config: SessionConfig) -> AsyncSession[Response]:
        """Get or create a session for the given configuration.

        :param config: Session configuration specifying proxy, connection limits, etc.
        :returns: AsyncSession instance ready for requests
        :raises SessionManagerError: If session creation fails repeatedly
        """
        # Start cleanup task if not already started (lazy initialization)
        self._start_cleanup_task()

        # Check if existing session is healthy
        if config in self._sessions:
            health = self._session_health.get(config)
            if health and health.is_healthy():
                health.record_success()
                return self._sessions[config]
            else:
                # Remove unhealthy session
                await self._close_session(config)

        # Create new session with per-config locking
        async with self._locks[config]:
            # Double-check pattern - another coroutine might have created it
            if config in self._sessions:
                health = self._session_health.get(config)
                if health and health.is_healthy():
                    health.record_success()
                    return self._sessions[config]

            # Create new session
            try:
                session = self._create_session(config)
                self._sessions[config] = session
                self._session_health[config] = SessionHealth()
                return session
            except Exception as e:
                raise SessionManagerError(f"Failed to create session: {e}") from e

    def _create_session(self, config: SessionConfig) -> AsyncSession[Response]:
        """Create a new AsyncSession with the specified configuration.

        :param config: Configuration for the new session
        :returns: Configured AsyncSession instance
        """
        # Configure session parameters based on config
        session_kwargs: dict[str, Any] = {
            "impersonate": config.impersonate,
            "timeout": 30,  # Default request timeout
        }

        # Add proxy if specified
        if config.proxy_url:
            session_kwargs["proxy"] = config.proxy_url

        # Note: curl_cffi doesn't expose connection pool configuration directly
        # Connection pooling is handled internally by curl_cffi

        return AsyncSession[Response](**session_kwargs)

    async def handle_request_error(
        self, config: SessionConfig, error: Exception
    ) -> bool:
        """Handle request error and determine if session should be recreated.

        :param config: Configuration of the session that encountered the error
        :param error: The exception that occurred
        :returns: True if session was invalidated and should be recreated
        """
        health = self._session_health.get(config)
        if health:
            health.record_error()

            # Invalidate session on connection-level errors
            if isinstance(
                error, (ConnectionError, ConnectionResetError, RequestException)
            ):
                await self._close_session(config)
                return True

        return False

    async def close_all(self) -> None:
        """Close all managed sessions and cleanup resources."""
        # Cancel cleanup task
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Close all sessions
        for config in list(self._sessions.keys()):
            await self._close_session(config)

        self._sessions.clear()
        self._session_health.clear()
        self._locks.clear()

    def get_metrics(self) -> dict[str, Any]:
        """Get connection pool metrics for monitoring.

        :returns: Dictionary containing session metrics
        """
        total_sessions = len(self._sessions)
        total_requests = sum(
            health.request_count for health in self._session_health.values()
        )
        total_errors = sum(
            health.error_count for health in self._session_health.values()
        )

        return {
            "active_sessions": total_sessions,
            "total_requests": total_requests,
            "total_errors": total_errors,
            "error_rate": total_errors / max(total_requests, 1),
            "configurations": len(set(self._sessions.keys())),
        }
