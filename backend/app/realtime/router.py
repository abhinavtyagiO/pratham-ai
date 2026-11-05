"""Authenticated Exotel ingress, finite handshake validation, and process-local admission."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.api.security import valid_exotel_auth
from app.realtime.protocol import ExotelProtocol, ProtocolError
from app.realtime.session import RealtimeSession

router = APIRouter()


class SessionRegistry:
    """Limit active handshakes/sessions and reject duplicate call SIDs within one process. Own
    an async lock, connection counter, and claimed-call set. The router releases
    reservations and claims in finally; multi-process admission and reconnection are outside
    this pilot.
    """

    def __init__(self, limit):
        """Initialize the process-local connection limit, lock, counter, and active call
        identities. No cross-process or persistent lock is implied.
        """
        self.limit = limit
        self.lock = asyncio.Lock()
        self.connections = 0
        self.calls = set()

    @asynccontextmanager
    async def reserve(self):
        """Reserve capacity before WebSocket acceptance and release it on every exit, including
        cancellation. Lock only counter updates; raise CAPACITY_REACHED without waiting when
        the configured limit is full.
        """
        async with self.lock:
            if self.connections >= self.limit:
                raise ProtocolError("CAPACITY_REACHED")
            self.connections += 1
        try:
            yield
        finally:
            async with self.lock:
                self.connections -= 1

    async def claim(self, call_sid):
        """Atomically claim a provider call SID after start validation, raising DUPLICATE_CALL
        if active. The router owns release in finally, independently of the connection
        reservation.
        """
        async with self.lock:
            if call_sid in self.calls:
                raise ProtocolError("DUPLICATE_CALL")
            self.calls.add(call_sid)


@router.websocket("/voice/exotel")
async def exotel(socket: WebSocket):
    """Authenticate and own one Exotel WebSocket from pre-upgrade validation to cleanup.
    Require enabled ingress and Basic auth, reserve capacity, parse a bounded
    connected/start handshake, and verify account/destination before creating per-call
    providers. Delegate runtime to RealtimeSession; close protocol/timeouts/disconnects
    safely and always release claims. No customer metadata grants authorization.
    """
    state = socket.app.state
    settings = state.voice_settings
    if not settings.enabled or not valid_exotel_auth(
        socket.headers.get("authorization", ""), settings
    ):
        await socket.close(code=1008)
        return
    claimed = None
    try:
        async with state.voice_registry.reserve():
            await socket.accept()
            protocol = ExotelProtocol()
            async with asyncio.timeout(settings.handshake_timeout_s):
                for _ in range(2):
                    protocol.parse(await socket.receive_text())
                    if protocol.start:
                        break
            info = protocol.start
            if info is None or (
                settings.exotel_account_sid and info.account_sid != settings.exotel_account_sid
            ):
                raise ProtocolError("INVALID_ACCOUNT")
            if settings.exotel_to_number and info.to_number != settings.exotel_to_number:
                raise ProtocolError("INVALID_DESTINATION")
            await state.voice_registry.claim(info.call_sid)
            claimed = info.call_sid
            providers = state.voice_factory()
            session = RealtimeSession(
                socket, protocol, settings, providers, state.sessions, state.telemetry
            )
            await session.run()
    except (ProtocolError, TimeoutError, WebSocketDisconnect):
        try:
            await socket.close(code=1008)
        except RuntimeError:
            pass
    finally:
        if claimed:
            async with state.voice_registry.lock:
                state.voice_registry.calls.discard(claimed)
