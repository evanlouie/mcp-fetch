import asyncio
from datetime import datetime, timedelta

import pytest
from curl_cffi.requests.exceptions import RequestException

from .session_manager import SessionConfig, SessionManager


class TestSessionManager:
    """Test the SessionManager connection pooling functionality."""

    @pytest.mark.asyncio
    async def test_session_creation_and_reuse(self) -> None:
        """Test that sessions are created and reused properly."""
        session_manager = SessionManager()

        try:
            config = SessionConfig(impersonate="chrome", proxy_url=None)

            # Get first session
            session1 = await session_manager.get_session(config)
            assert session1 is not None

            # Get second session with same config - should be same instance
            session2 = await session_manager.get_session(config)
            assert session2 is session1, "Sessions should be reused for same config"

            assert len(session_manager._sessions) == 1

        finally:
            await session_manager.close_all()

    @pytest.mark.asyncio
    async def test_different_configs_different_sessions(self) -> None:
        """Test that different configurations get different sessions."""
        session_manager = SessionManager()

        try:
            config1 = SessionConfig(impersonate="chrome", proxy_url=None)
            config2 = SessionConfig(impersonate="chrome", proxy_url="http://proxy:8080")

            # Get sessions for different configs
            session1 = await session_manager.get_session(config1)
            session2 = await session_manager.get_session(config2)

            assert session1 is not session2, (
                "Different configs should get different sessions"
            )

            assert len(session_manager._sessions) == 2

        finally:
            await session_manager.close_all()

    @pytest.mark.asyncio
    async def test_session_health_tracking(self) -> None:
        """Test that session health is tracked properly."""
        session_manager = SessionManager()

        try:
            config = SessionConfig(impersonate="chrome", proxy_url=None)

            # Get session and verify health tracking
            session = await session_manager.get_session(config)
            assert session is not None

            # Check initial health state
            health = session_manager._session_health.get(config)
            assert health is not None
            assert health.request_count == 0  # Initial state for new session
            assert health.error_count == 0
            assert health.is_healthy()

        finally:
            await session_manager.close_all()

    @pytest.mark.asyncio
    async def test_error_handling_and_recreation(self) -> None:
        """Test that sessions are recreated after connection errors."""
        session_manager = SessionManager()

        try:
            config = SessionConfig(impersonate="chrome", proxy_url=None)

            # Get initial session
            session1 = await session_manager.get_session(config)
            assert session1 is not None

            # Simulate connection error
            error = RequestException("Connection failed")

            should_recreate = await session_manager.handle_request_error(config, error)
            assert should_recreate, "Connection errors should trigger recreation"

            # Get session again - should be a new instance
            session2 = await session_manager.get_session(config)
            assert session2 is not session1, (
                "New session should be created after connection error"
            )

        finally:
            await session_manager.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_and_resource_management(self) -> None:
        """Test that cleanup properly closes all sessions."""
        session_manager = SessionManager()

        config = SessionConfig(impersonate="chrome", proxy_url=None)

        # Create session
        session = await session_manager.get_session(config)
        assert session is not None

        # Verify session is active
        assert len(session_manager._sessions) == 1

        # Close all sessions
        await session_manager.close_all()

        # Verify cleanup
        assert len(session_manager._sessions) == 0
        assert len(session_manager._session_health) == 0

    @pytest.mark.asyncio
    async def test_concurrent_access(self) -> None:
        """Test that concurrent access to SessionManager works correctly."""
        session_manager = SessionManager()

        try:
            config = SessionConfig(impersonate="chrome", proxy_url=None)

            # Create multiple concurrent requests for the same session
            async def get_session_task():
                return await session_manager.get_session(config)

            tasks = [get_session_task() for _ in range(5)]
            sessions = await asyncio.gather(*tasks)

            # All should return the same session instance
            first_session = sessions[0]
            for session in sessions[1:]:
                assert session is first_session, (
                    "Concurrent access should return same session"
                )

            assert len(session_manager._sessions) == 1

        finally:
            await session_manager.close_all()

    def test_session_config_hashable(self) -> None:
        """Test that SessionConfig can be used as dictionary keys."""
        config1 = SessionConfig(impersonate="chrome", proxy_url=None)
        config2 = SessionConfig(impersonate="chrome", proxy_url=None)
        config3 = SessionConfig(impersonate="firefox", proxy_url=None)

        # Same configs should be equal and have same hash
        assert config1 == config2
        assert hash(config1) == hash(config2)

        # Different configs should not be equal
        assert config1 != config3

        # Should be usable as dict keys
        session_dict = {config1: "session1", config3: "session3"}
        assert session_dict[config2] == "session1"  # config2 == config1

    @pytest.mark.asyncio
    async def test_lock_cleanup_prevents_memory_leak(self) -> None:
        """Test that locks are properly cleaned up to prevent memory leaks."""
        session_manager = SessionManager()

        try:
            config1 = SessionConfig(impersonate="chrome", proxy_url=None)
            config2 = SessionConfig(impersonate="chrome", proxy_url="http://proxy:8080")

            # Create sessions to generate locks
            _ = await session_manager.get_session(config1)
            _ = await session_manager.get_session(config2)

            # Verify locks are created
            assert len(session_manager._locks) == 2
            assert config1 in session_manager._locks
            assert config2 in session_manager._locks

            # Close one session manually
            await session_manager._close_session(config1)

            # Verify the lock for config1 is cleaned up
            assert config1 not in session_manager._locks
            assert config1 not in session_manager._sessions
            assert config1 not in session_manager._session_health

            # Verify config2 resources are still present
            assert config2 in session_manager._locks
            assert config2 in session_manager._sessions
            assert config2 in session_manager._session_health

        finally:
            await session_manager.close_all()

            # Verify all locks are cleaned up after close_all
            assert len(session_manager._locks) == 0

    @pytest.mark.asyncio
    async def test_cleanup_respects_recent_activity(self) -> None:
        """Old but recently used sessions should not be eagerly closed."""
        session_manager = SessionManager()

        try:
            config = SessionConfig()
            session = await session_manager.get_session(config)
            assert session is not None

            health = session_manager._session_health[config]
            # Simulate a session created long ago but used moments ago
            health.created_at -= timedelta(hours=2)
            health.last_used = datetime.now()

            await session_manager._cleanup_unhealthy_sessions()
            assert config in session_manager._sessions

            # Now mark it idle beyond the grace period
            health.last_used -= timedelta(minutes=10)
            await session_manager._cleanup_unhealthy_sessions()
            assert config not in session_manager._sessions

        finally:
            await session_manager.close_all()
