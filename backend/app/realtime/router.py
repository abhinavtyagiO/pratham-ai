import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.api.security import valid_exotel_auth
from app.realtime.protocol import ExotelProtocol, ProtocolError
from app.realtime.session import RealtimeSession

router = APIRouter()


class SessionRegistry:
    def __init__(self, limit):
        self.limit = limit
        self.lock = asyncio.Lock()
        self.connections = 0
        self.calls = set()

    @asynccontextmanager
    async def reserve(self):
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
        async with self.lock:
            if call_sid in self.calls:
                raise ProtocolError("DUPLICATE_CALL")
            self.calls.add(call_sid)


@router.websocket("/voice/exotel")
async def exotel(socket: WebSocket):
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
