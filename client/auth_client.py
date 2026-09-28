"""Session and token lifecycle.

The server's tokens (application/auth_service.py) are opaque 32-byte hex
strings held in an in-memory dict with no expiry field, so the client cannot
inspect a token to know whether it is still good -- it only finds out when a
call is rejected. ``SessionManager`` therefore treats "the server said
unauthenticated" as the single source of truth and, if credentials were
supplied, transparently re-logs-in once and lets the caller replay.

Credentials are held in memory only, never written to disk.
"""

from __future__ import annotations

import logging
import threading

from client.errors import AuctionError, ErrorKind, classify_status
from client.resilience import IDEMPOTENT, NodePool, call
from client.state import Store

log = logging.getLogger("auction.client.auth")


class SessionManager:
    """Owns the token. Thread-safe: the watcher and UI share one instance."""

    def __init__(self, pool: NodePool, store: Store, *, remember_credentials: bool = True) -> None:
        self._pool = pool
        self._store = store
        self._remember = remember_credentials
        self._lock = threading.RLock()
        self._token: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        # Bumped on every successful login. Lets concurrent callers that all
        # hit UNAUTHENTICATED at once collapse into a single re-login instead
        # of stampeding the server with N logins.
        self._generation = 0

    # -- accessors ------------------------------------------------------------

    @property
    def token(self) -> str | None:
        with self._lock:
            return self._token

    @property
    def username(self) -> str | None:
        with self._lock:
            return self._username

    @property
    def authenticated(self) -> bool:
        return self.token is not None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def require_token(self) -> str:
        token = self.token
        if token is None:
            raise AuctionError(ErrorKind.UNAUTHENTICATED, "Not logged in")
        return token

    # -- login / logout -------------------------------------------------------

    def login(self, username: str, password: str) -> str:
        from generated import auction_pb2

        def invoke(stub, endpoint):
            reply = stub.Login(
                auction_pb2.LoginRequest(username=username, password=password),
                timeout=self._pool._config.rpc_timeout_s,
            )
            if not reply.status.success:
                # Bad credentials are a permanent failure -- raising a
                # non-retryable error stops the retry loop hammering login.
                raise classify_status(reply.status.message, endpoint=endpoint)
            return reply

        reply = call(self._pool, IDEMPOTENT("Login"), invoke, config=self._pool._config)

        with self._lock:
            self._token = reply.token
            self._username = username
            self._password = password if self._remember else None
            self._generation += 1

        self._store.set_session(username, reply.token)
        log.info("logged in as %s", username)
        return reply.token

    def logout(self) -> bool:
        from generated import auction_pb2

        token = self.token
        if token is None:
            return False

        def invoke(stub, endpoint):
            return stub.Logout(
                auction_pb2.LogoutRequest(token=token),
                timeout=self._pool._config.rpc_timeout_s,
            )

        try:
            reply = call(self._pool, IDEMPOTENT("Logout"), invoke, config=self._pool._config)
            success = reply.success
        except AuctionError as exc:
            # The local session is dropped regardless: a logout that cannot
            # reach the server still has to log the user out of this client.
            log.warning("logout RPC failed (%s); clearing local session anyway", exc.kind.value)
            success = False
        finally:
            self.clear()

        return success

    def clear(self) -> None:
        with self._lock:
            self._token = None
            self._username = None
            self._password = None
        self._store.clear_session()

    # -- re-authentication ----------------------------------------------------

    def can_reauth(self) -> bool:
        with self._lock:
            return bool(self._username and self._password)

    def reauth(self, seen_generation: int) -> bool:
        """Re-login after an UNAUTHENTICATED reply.

        ``seen_generation`` is the generation the caller was using when it
        failed. If the token has already been refreshed by someone else since
        then, this is a no-op success and the caller simply retries with the
        newer token.
        """
        with self._lock:
            if seen_generation != self._generation:
                return True  # somebody else already refreshed it
            username, password = self._username, self._password

        if not (username and password):
            return False

        try:
            self.login(username, password)
            log.info("re-authenticated as %s", username)
            return True
        except AuctionError as exc:
            log.error("re-authentication failed: %s", exc)
            self.clear()
            return False
