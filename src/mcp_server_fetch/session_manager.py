import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests.exceptions import RequestException


@dataclass(frozen=True)
class SessionConfig:
    """Hashable key for session caching."""

    impersonate: str = "chrome"
    proxy_url: str | None = None


SESSION_MAX_AGE = timedelta(hours=1)
SESSION_IDLE_GRACE_PERIOD = timedelta(minutes=5)
SESSION_MAX_ERRORS = 3


@dataclass
class SessionHealth:
    """Tracks session health metrics for automatic recreation."""

    last_used: datetime = field(default_factory=datetime.now)
    error_count: int = 0
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

    def record_use(self) -> None:
        """Record that the session was accessed."""
        self.last_used = datetime.now()

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
        unhealthy_configs: list[SessionConfig] = []

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

        _ = self._session_health.pop(config, None)

        _ = self._locks.pop(config, None)

    async def get_session(self, config: SessionConfig) -> AsyncSession[Response]:
        """Get or create a session for the given configuration.

        :param config: Session configuration specifying proxy, connection limits, etc.
        :returns: AsyncSession instance ready for requests
        :raises SessionManagerError: If session creation fails repeatedly
        """
        self._start_cleanup_task()

        if config in self._sessions:
            health = self._session_health.get(config)
            if health and health.is_healthy():
                health.record_use()
                return self._sessions[config]

        async with self._locks[config]:
            if config in self._sessions:
                health = self._session_health.get(config)
                if health and health.is_healthy():
                    health.record_use()
                    return self._sessions[config]
                else:
                    await self._close_session(config)

            try:
                session = self._create_session(config)
                self._sessions[config] = session
                self._session_health[config] = SessionHealth()
                return session
            except Exception as e:
                _ = self._locks.pop(config, None)
                raise SessionManagerError(f"Failed to create session: {e}") from e

    def _create_session(self, config: SessionConfig) -> AsyncSession[Response]:
        """Create a new AsyncSession with the specified configuration.

        :param config: Configuration for the new session
        :returns: Configured AsyncSession instance
        """
        session_kwargs: dict[str, Any] = {
            "impersonate": config.impersonate,
        }

        if config.proxy_url:
            session_kwargs["proxy"] = config.proxy_url

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
        if self._cleanup_task and not self._cleanup_task.done():
            _ = self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        for config in list(self._sessions.keys()):
            await self._close_session(config)

