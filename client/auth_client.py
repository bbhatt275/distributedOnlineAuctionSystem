"""Session and token handling.

Server tokens have no expiry field we can inspect (auth_service.py just keeps
them in a dict), so the client only discovers a dead token when a call is
rejected. If credentials were supplied we log in again and let the caller
replay. Credentials stay in memory, never on disk.
"""

from __future__ import annotations

import logging
import threading

from client.errors import AuctionError, ErrorKind, classify_status
from client.resilience import IDEMPOTENT, call

log = logging.getLogger("auction.client.auth")


class SessionManager:

    def __init__(self, pool, store, remember_credentials=True):
        self._pool = pool
        self._store = store
        self._remember = remember_credentials
        self._lock = threading.RLock()
        self._token = None
        self._username = None
        self._password = None
        # This counter increases on each login. It allows several callers that
        # meet the same expired token to share one re-login between them.
        self._generation = 0

    @property
    def token(self):
        with self._lock:
            return self._token

    @property
    def username(self):
        with self._lock:
            return self._username

    @property
    def authenticated(self):
        return self.token is not None

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def require_token(self):
        token = self.token
        if token is None:
            raise AuctionError(ErrorKind.UNAUTHENTICATED, "Not logged in")
        return token

    def login(self, username, password):
        from generated import auction_pb2

        def invoke(stub, endpoint):
            reply = stub.Login(
                auction_pb2.LoginRequest(username=username, password=password),
                timeout=self._pool._config.rpc_timeout_s,
            )
            if not reply.status.success:
                # Raising a permanent failure stops the retry loop, which
                # would otherwise repeat credentials that are simply wrong.
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

    def logout(self):
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
            # The local session is cleared regardless, because a sign out that
            # cannot reach the server should still sign the user out here.
            log.warning("logout RPC failed (%s); clearing session locally", exc.kind.value)
            success = False
        finally:
            self.clear()

        return success

    def clear(self):
        with self._lock:
            self._token = None
            self._username = None
            self._password = None
        self._store.clear_session()

    def can_reauth(self):
        with self._lock:
            return bool(self._username and self._password)

    def reauth(self, seen_generation):
        """Log in again after a rejected token.

        seen_generation is what the caller was using when it failed; if the
        token has already been refreshed since, this is a no-op and the caller
        just retries with the newer one.
        """
        with self._lock:
            if seen_generation != self._generation:
                return True
            username, password = self._username, self._password

        if not (username and password):
            return False

        try:
            self.login(username, password)
            return True
        except AuctionError as exc:
            log.error("re-authentication failed: %s", exc)
            self.clear()
            return False
