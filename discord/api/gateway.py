import asyncio
from collections import namedtuple, deque
import concurrent.futures
import logging
import struct
import sys
import time
import threading
import traceback
import zlib

import aiohttp

from random import random
from typing import Any, Optional, Union

from orjson import dumps, loads

from ..base import Data
from ..enums import InteractionType
from ..models.misc import InteractionData
from .dispatch import Listener
from .enums import OpCodeType
from .error import GatewayException
from .errors import ConnectionClosed, InvalidArgument
from .models.channel import Channel
from .models.intents import Intents
from .models.member import Member
from .models.message import Message
from .models.user import User
from . import utils

logging.basicConfig(level=Data.LOGGER)
log: logging.Logger = logging.getLogger("gateway")

__all__ = (
    'DiscordWebSocket',
    'KeepAliveHandler',
    'ReconnectWebSocket',
)


class GatewayRatelimiter:
    def __init__(self, count=110, per=60.0):
        # The default is 110 to give room for at least 10 heartbeats per minute
        self.max = count
        self.remaining = count
        self.window = 0.0
        self.per = per
        self.lock = asyncio.Lock()
        self.shard_id = None

    def is_ratelimited(self):
        current = time.time()
        if current > self.window + self.per:
            return False
        return self.remaining == 0

    def get_delay(self):
        current = time.time()

        if current > self.window + self.per:
            self.remaining = self.max

        if self.remaining == self.max:
            self.window = current

        if self.remaining == 0:
            return self.per - (current - self.window)

        self.remaining -= 1
        if self.remaining == 0:
            self.window = current

        return 0.0

    async def block(self):
        async with self.lock:
            delta = self.get_delay()
            if delta:
                log.warning('WebSocket in shard ID %s is ratelimited, waiting %.2f seconds', self.shard_id, delta)
                await asyncio.sleep(delta)

class ReconnectWebSocket(Exception):
    """Signals to safely reconnect the websocket."""
    def __init__(self, shard_id, *, resume=True):
        self.shard_id = shard_id
        self.resume = resume
        self.op = 'RESUME' if resume else 'IDENTIFY'

class WebSocketClosure(Exception):
    """An exception to make up for the fact that aiohttp doesn't signal closure."""
    pass

EventListener = namedtuple('EventListener', 'predicate event result future')

class KeepAliveHandler(threading.Thread):
    def __init__(self, *args, **kwargs):
        ws = kwargs.pop('ws', None)
        interval = kwargs.pop('interval', None)
        shard_id = kwargs.pop('shard_id', None)
        threading.Thread.__init__(self, *args, **kwargs)
        self.ws = ws
        self._main_thread_id = ws.thread_id
        self.interval = interval
        self.daemon = True
        self.shard_id = shard_id
        self.msg = 'Keeping shard ID %s websocket alive with sequence %s.'
        self.block_msg = 'Shard ID %s heartbeat blocked for more than %s seconds.'
        self.behind_msg = 'Can\'t keep up, shard ID %s websocket is %.1fs behind.'
        self._stop_ev = threading.Event()
        self._last_ack = time.perf_counter()
        self._last_send = time.perf_counter()
        self._last_recv = time.perf_counter()
        self.latency = float('inf')
        self.heartbeat_timeout = ws._max_heartbeat_timeout

    def run(self):
        while not self._stop_ev.wait(self.interval):
            if self._last_recv + self.heartbeat_timeout < time.perf_counter():
                log.warning("Shard ID %s has stopped responding to the gateway. Closing and restarting.", self.shard_id)
                coro = self.ws.close(4000)
                f = asyncio.run_coroutine_threadsafe(coro, loop=self.ws.loop)

                try:
                    f.result()
                except Exception:
                    log.exception('An error occurred while stopping the gateway. Ignoring.')
                finally:
                    self.stop()
                    return

            data = self.get_payload()
            log.debug(self.msg, self.shard_id, data['d'])
            coro = self.ws.send_heartbeat(data)
            f = asyncio.run_coroutine_threadsafe(coro, loop=self.ws.loop)
            try:
                # block until sending is complete
                total = 0
                while True:
                    try:
                        f.result(10)
                        break
                    except concurrent.futures.TimeoutError:
                        total += 10
                        try:
                            frame = sys._current_frames()[self._main_thread_id]
                        except KeyError:
                            msg = self.block_msg
                        else:
                            stack = ''.join(traceback.format_stack(frame))
                            msg = f'{self.block_msg}\nLoop thread traceback (most recent call last):\n{stack}'
                        log.warning(msg, self.shard_id, total)

            except Exception:
                self.stop()
            else:
                self._last_send = time.perf_counter()

    def get_payload(self):
        return {
            'op': int(OpCodeType.HEARTBEAT),
            'd': self.ws.sequence
        }

    def stop(self):
        self._stop_ev.set()

    def tick(self):
        self._last_recv = time.perf_counter()

    def ack(self):
        ack_time = time.perf_counter()
        self._last_ack = ack_time
        self.latency = ack_time - self._last_send
        if self.latency > 10:
            log.warning(self.behind_msg, self.shard_id, self.latency)

class DiscordClientWebSocketResponse(aiohttp.ClientWebSocketResponse):
    async def close(self, *, code: int = 4000, message: bytes = b'') -> bool:
        return await super().close(code=code, message=message)

class DiscordWebSocket:
    """
    A class representing a websocket connection with the gateway.

    :ivar interactions.api.models.intents.Intents intents: An instance of :class:`interactions.api.models.Intents`.
    :ivar asyncio.AbstractEventLoop loop: The coroutine event loop established on.
    :ivar interactions.api.http.Request req: An instance of :class:`interactions.api.http.Request`.
    :ivar interactions.api.dispatch.Listener dispatch: An instance of :class:`interactions.api.dispatch.Listener`.
    :ivar typing.Any session: The current client session.
    :ivar int session_id: The current ID of the gateway session.
    :ivar int sequence: The current sequence of the gateway connection.
    :ivar interactions.api.gateway.Heartbeat keep_alive: An instance of :class:`interactions.api.gateway.Heartbeat`.
    :ivar bool closed: The current connection state.
    :ivar interactions.api.http.HTTPClient http: The internal HTTP client used to connect to the gateway.
    :ivar dict options: The websocket connection options.
    """

    def __init__(self, socket, *, loop):
        self.socket = socket
        self.loop = loop

        # an empty dispatcher to prevent crashes
        self._dispatch = lambda *args: None
        # generic event listeners
        self._dispatch_listeners = []
        # the keep alive
        self._keep_alive = None
        self.thread_id = threading.get_ident()
        self._max_heartbeat_timeout = 60.0

        # ws related stuff
        self.session_id = None
        self.sequence = None
        self._zlib = zlib.decompressobj()
        self._buffer = bytearray()
        self._close_code = None
        self._rate_limiter = GatewayRatelimiter()

    @property
    def open(self):
        return not self.socket.closed

    @classmethod
    async def from_client(cls, client, *, initial=False, gateway=None, shard_id=None, session=None, sequence=None, resume=False):
        """Creates a main websocket for Discord from a :class:`Client`.

        This is for internal use only.
        """
        gateway = gateway or await client.http.get_gateway()
        socket = await client.http.ws_connect(gateway)
        ws = cls(socket, loop=client.loop)

        # dynamically add attributes needed
        ws.token = client.http.token
        ws._dispatch = client.dispatch
        ws.gateway = gateway
        ws._initial_identify = initial
        ws.shard_id = shard_id
        ws._rate_limiter.shard_id = shard_id
        ws.session_id = session
        ws.sequence = sequence

        #client._connection._update_references(ws)

        log.debug('Created websocket connected to %s', gateway)

        # poll event for OP Hello
        await ws.poll_event()

        if not resume:
            await ws.identify()
            return ws

        await ws.resume()
        return ws

    def wait_for(self, event, predicate, result=None):
        """Waits for a DISPATCH'd event that meets the predicate.

        Parameters
        -----------
        event: :class:`str`
            The event name in all upper case to wait for.
        predicate
            A function that takes a data parameter to check for event
            properties. The data parameter is the 'd' key in the JSON message.
        result
            A function that takes the same data parameter and executes to send
            the result to the future. If ``None``, returns the data.

        Returns
        --------
        asyncio.Future
            A future to wait for.
        """

        future = self.loop.create_future()
        entry = EventListener(event=event, predicate=predicate, result=result, future=future)
        self._dispatch_listeners.append(entry)
        return future

    async def identify(self):
        """Sends the IDENTIFY packet."""
        payload = {
            'op': OpCodeType.IDENTIFY,
            'd': {
                'token': self.token,
                'properties': {
                    '$os': sys.platform,
                    '$browser': 'discord-continued',
                    '$device': 'discord-continued',
                    '$referrer': '',
                    '$referring_domain': ''
                },
                'compress': True,
                'large_threshold': 250,
                'v': 3,
                'intents': Intents.ALL
            }
        }

        if self.shard_id is not None and self.shard_count is not None:
            payload['d']['shard'] = [self.shard_id, self.shard_count]

        #await self.call_hooks('before_identify', self.shard_id, initial=self._initial_identify)
        await self.send_as_json(payload)
        log.info('Shard ID %s has sent the IDENTIFY payload.', self.shard_id)

    async def resume(self):
        """Sends the RESUME packet."""
        payload = {
            'op': OpCodeType.RESUME,
            'd': {
                'seq': self.sequence,
                'session_id': self.session_id,
                'token': self.token
            }
        }

        await self.send_as_json(payload)
        log.info('Shard ID %s has sent the RESUME payload.', self.shard_id)

    async def received_message(self, msg, /):
        if type(msg) is bytes:
            self._buffer.extend(msg)

            if len(msg) < 4 or msg[-4:] != b'\x00\x00\xff\xff':
                return
            msg = self._zlib.decompress(self._buffer)
            msg = msg.decode('utf-8')
            self._buffer = bytearray()
        msg = utils.from_json(msg)

        log.debug('For Shard ID %s: WebSocket Event: %s', self.shard_id, msg)
        event = msg.get('t')
        if event:
            self._dispatch('socket_event_type', event)

        op = msg.get('op')
        data = msg.get('d')
        seq = msg.get('s')
        if seq is not None:
            self.sequence = seq

        if self._keep_alive:
            self._keep_alive.tick()

        if op != OpCodeType.DISPATCH:
            if op == OpCodeType.RECONNECT:
                # "reconnect" can only be handled by the Client
                # so we terminate our connection and raise an
                # internal exception signalling to reconnect.
                log.debug('Received RECONNECT opcode.')
                await self.close()
                raise ReconnectWebSocket(self.shard_id)

            if op == OpCodeType.HEARTBEAT_ACK:
                if self._keep_alive:
                    self._keep_alive.ack()
                return

            if op == OpCodeType.HEARTBEAT:
                if self._keep_alive:
                    beat = self._keep_alive.get_payload()
                    await self.send_as_json(beat)
                return

            if op == OpCodeType.HELLO:
                interval = data['heartbeat_interval'] / 1000.0
                self._keep_alive = KeepAliveHandler(ws=self, interval=interval, shard_id=self.shard_id)
                # send a heartbeat immediately
                await self.send_as_json(self._keep_alive.get_payload())
                self._keep_alive.start()
                return

            if op == OpCodeType.INVALIDATE_SESSION:
                if data is True:
                    await self.close()
                    raise ReconnectWebSocket(self.shard_id)

                self.sequence = None
                self.session_id = None
                log.info('Shard ID %s session has been invalidated.', self.shard_id)
                await self.close(code=1000)
                raise ReconnectWebSocket(self.shard_id, resume=False)

            log.warning('Unknown OP code %s.', op)
            return

        if event == 'READY':
            self._trace = trace = data.get('_trace', [])
            self.sequence = msg['s']
            self.session_id = data['session_id']
            # pass back shard ID to ready handler
            data['__shard_id__'] = self.shard_id
            log.info('Shard ID %s has connected to Gateway: %s (Session ID: %s).',
                     self.shard_id, ', '.join(trace), self.session_id)

        elif event == 'RESUMED':
            self._trace = trace = data.get('_trace', [])
            # pass back the shard ID to the resumed handler
            data['__shard_id__'] = self.shard_id
            log.info('Shard ID %s has successfully RESUMED session %s under trace %s.',
                     self.shard_id, self.session_id, ', '.join(trace))

        data['event_name'] = event

        try:
            self._dispatch(event.lower(), data)
        except:
            log.debug('Unknown event %s.', event)
        else:
            self._dispatch('any', data)

        # remove the dispatched listeners
        removed = []
        for index, entry in enumerate(self._dispatch_listeners):
            if entry.event != event:
                continue

            future = entry.future
            if future.cancelled():
                removed.append(index)
                continue

            try:
                valid = entry.predicate(data)
            except Exception as exc:
                future.set_exception(exc)
                removed.append(index)
            else:
                if valid:
                    ret = data if entry.result is None else entry.result(data)
                    future.set_result(ret)
                    removed.append(index)

        for index in reversed(removed):
            del self._dispatch_listeners[index]

    @property
    def latency(self):
        """:class:`float`: Measures latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds."""
        heartbeat = self._keep_alive
        return float('inf') if heartbeat is None else heartbeat.latency

    def _can_handle_close(self):
        code = self._close_code or self.socket.close_code
        return code not in (1000, 4004, 4010, 4011, 4012, 4013, 4014)

    async def poll_event(self):
        """Polls for a DISPATCH event and handles the general gateway loop.

        Raises
        ------
        ConnectionClosed
            The websocket connection was terminated for unhandled reasons.
        """
        try:
            msg = await self.socket.receive(timeout=self._max_heartbeat_timeout)
            if msg.type is aiohttp.WSMsgType.TEXT:
                await self.received_message(msg.data)
            elif msg.type is aiohttp.WSMsgType.BINARY:
                await self.received_message(msg.data)
            elif msg.type is aiohttp.WSMsgType.ERROR:
                log.debug('Received %s', msg)
                raise msg.data
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSE):
                log.debug('Received %s', msg)
                raise WebSocketClosure
        except (asyncio.TimeoutError, WebSocketClosure) as e:
            # Ensure the keep alive handler is closed
            if self._keep_alive:
                self._keep_alive.stop()
                self._keep_alive = None

            if isinstance(e, asyncio.TimeoutError):
                log.info('Timed out receiving packet. Attempting a reconnect.')
                raise ReconnectWebSocket(self.shard_id) from None

            code = self._close_code or self.socket.close_code
            if self._can_handle_close():
                log.info('Websocket closed with %s, attempting a reconnect.', code)
                raise ReconnectWebSocket(self.shard_id) from None
            else:
                log.info('Websocket closed with %s, cannot reconnect.', code)
                raise ConnectionClosed(self.socket, shard_id=self.shard_id, code=code) from None

    async def send(self, data):
        await self._rate_limiter.block()
        self._dispatch('socket_raw_send', data)
        await self.socket.send_str(data)

    async def send_as_json(self, data):
        try:
            await self.send(utils.to_json(data))
        except RuntimeError as exc:
            if not self._can_handle_close():
                raise ConnectionClosed(self.socket, shard_id=self.shard_id) from exc

    async def send_heartbeat(self, data):
        # This bypasses the rate limit handling code since it has a higher priority
        try:
            await self.socket.send_str(utils.to_json(data))
        except RuntimeError as exc:
            if not self._can_handle_close():
                raise ConnectionClosed(self.socket, shard_id=self.shard_id) from exc

    async def change_presence(self, *, activity=None, status=None, afk=False, since=0.0):
        if activity is not None:
            if not isinstance(activity, BaseActivity):
                raise InvalidArgument('activity must derive from BaseActivity.')
            activity = activity.to_dict()

        if status == 'idle':
            since = int(time.time() * 1000)

        payload = {
            'op': self.PRESENCE,
            'd': {
                'game': activity,
                'afk': afk,
                'since': since,
                'status': status
            }
        }

        sent = utils.to_json(payload)
        log.debug('Sending "%s" to change status', sent)
        await self.send(sent)

    async def request_sync(self, guild_ids):
        payload = {
            'op': self.GUILD_SYNC,
            'd': list(guild_ids)
        }
        await self.send_as_json(payload)

    async def request_chunks(self, guild_id, query=None, *, limit, user_ids=None, presences=False, nonce=None):
        payload = {
            'op': self.REQUEST_MEMBERS,
            'd': {
                'guild_id': guild_id,
                'presences': presences,
                'limit': limit
            }
        }

        if nonce:
            payload['d']['nonce'] = nonce

        if user_ids:
            payload['d']['user_ids'] = user_ids

        if query is not None:
            payload['d']['query'] = query


        await self.send_as_json(payload)

    async def voice_state(self, guild_id, channel_id, self_mute=False, self_deaf=False):
        payload = {
            'op': self.VOICE_STATE,
            'd': {
                'guild_id': guild_id,
                'channel_id': channel_id,
                'self_mute': self_mute,
                'self_deaf': self_deaf
            }
        }

        log.debug('Updating our voice state to %s.', payload)
        await self.send_as_json(payload)

    async def close(self, code=4000):
        if self._keep_alive:
            self._keep_alive.stop()
            self._keep_alive = None

        self._close_code = code
        await self.socket.close(code=code)
