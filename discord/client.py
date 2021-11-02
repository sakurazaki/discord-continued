import asyncio
import signal
import aiohttp
from logging import Logger, basicConfig, getLogger
from typing import Any, Callable, Coroutine, List, Optional, Union

from .api.cache import Cache, Item
from .api.error import InteractionException, JSONException
from .api.errors import *
from .api.gateway import DiscordWebSocket, ReconnectWebSocket
from .api.http import HTTPClient
from .api.models.guild import Guild
from .api.models.intents import Intents
from .api.models.team import Application
from .base import Data
from .backoff import ExponentialBackoff
from .enums import ApplicationCommandType
from .models.command import ApplicationCommand, Option

__all__ = (
    'Client',
)

# TODO: Find a better way to call on the cache
cache = Cache()

basicConfig(level=Data.LOGGER)
log: Logger = getLogger("client")


def _cancel_tasks(loop: asyncio.AbstractEventLoop) -> None:
    tasks = {t for t in asyncio.all_tasks(loop=loop) if not t.done()}

    if not tasks:
        return

    log.info('Cleaning up after %d tasks.', len(tasks))
    for task in tasks:
        task.cancel()

    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    log.info('All tasks finished cancelling.')

    for task in tasks:
        if task.cancelled():
            continue
        if task.exception() is not None:
            loop.call_exception_handler({
                'message': 'Unhandled exception during Client.run shutdown.',
                'exception': task.exception(),
                'task': task
            })

def _cleanup_loop(loop: asyncio.AbstractEventLoop) -> None:
    try:
        _cancel_tasks(loop)
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        log.info('Closing the event loop.')
        loop.close()

class Client:
    """
    A class representing the client connection to Discord's gateway and API via. WebSocket and HTTP.

    :ivar DiscordWebSocket ws: The Websocket we connect to
    :ivar asyncio.AbstractEventLoop loop: The main overall asynchronous coroutine loop in effect.
    """

    def __init__(self, *, loop: Optional[asyncio.AbstractEventLoop] = None, **options: Any,) -> None:
        self.ws = None
        self.loop = asyncio.get_event_loop() if loop is None else loop
        self._listeners = {}
        self.shard_id = options.get('shard_id')
        self.shard_count = options.get('shard_count')

        connector = options.pop('connector', None)
        proxy = options.pop('proxy', None)
        proxy_auth = options.pop('proxy_auth', None)
        unsync_clock = options.pop('assume_unsync_clock', True)
        self.http = HTTPClient(connector, proxy=proxy, proxy_auth=proxy_auth, unsync_clock=unsync_clock, loop=self.loop)

        self._handlers = {
            'ready': self._handle_ready
        }

        self._hooks = {
            'before_identify': self._call_before_identify_hook
        }

        self._closed = False
        self._ready = asyncio.Event()

    async def login(self, token, *, bot=True):
        """|coro|

        Logs in the client with the specified credentials.

        This function can be used in two different ways.

        .. warning::

            Logging on with a user token is against the Discord
            `Terms of Service <https://support.discord.com/hc/en-us/articles/115002192352>`_
            and doing so might potentially get your account banned.
            Use this at your own risk.

        Parameters
        -----------
        token: :class:`str`
            The authentication token. Do not prefix this token with
            anything as the library will do it for you.
        bot: :class:`bool`
            Keyword argument that specifies if the account logging on is a bot
            token or not.

            .. deprecated:: 1.7

        Raises
        ------
        :exc:`.LoginFailure`
            The wrong credentials are passed.
        :exc:`.HTTPException`
            An unknown HTTP related error occurred,
            usually when it isn't 200 or the known incorrect credentials
            passing status code.
        """

        log.info('logging in using static token')
        await self.http.static_login(token.strip(), bot=bot)

    async def connect(self, *, reconnect=True):
        """|coro|

        Creates a websocket connection and lets the websocket listen
        to messages from Discord. This is a loop that runs the entire
        event system and miscellaneous aspects of the library. Control
        is not resumed until the WebSocket connection is terminated.

        Parameters
        -----------
        reconnect: :class:`bool`
            If we should attempt reconnecting, either due to internet
            failure or a specific failure on Discord's part. Certain
            disconnects that lead to bad state will not be handled (such as
            invalid sharding payloads or bad tokens).

        Raises
        -------
        :exc:`.GatewayNotFound`
            If the gateway to connect to Discord is not found. Usually if this
            is thrown then there is a Discord API outage.
        :exc:`.ConnectionClosed`
            The websocket connection has been terminated.
        """

        backoff = ExponentialBackoff()
        ws_params = {
            'initial': True,
            'shard_id': self.shard_id,
        }
        while not self.is_closed():
            try:
                coro = DiscordWebSocket.from_client(self, **ws_params)
                self.ws = await asyncio.wait_for(coro, timeout=60.0)
                ws_params['initial'] = False
                while True:
                    await self.ws.poll_event()
            except ReconnectWebSocket as e:
                log.info('Got a request to %s the websocket.', e.op)
                self.dispatch('disconnect')
                ws_params.update(sequence=self.ws.sequence, resume=e.resume, session=self.ws.session_id)
                continue
            except (OSError,
                    HTTPException,
                    GatewayNotFound,
                    ConnectionClosed,
                    aiohttp.ClientError,
                    asyncio.TimeoutError) as exc:

                self.dispatch('disconnect')
                if not reconnect:
                    await self.close()
                    if isinstance(exc, ConnectionClosed) and exc.code == 1000:
                        # clean close, don't re-raise this
                        return
                    raise

                if self.is_closed():
                    return

                # If we get connection reset by peer then try to RESUME
                if isinstance(exc, OSError) and exc.errno in (54, 10054):
                    ws_params.update(sequence=self.ws.sequence, initial=False, resume=True, session=self.ws.session_id)
                    continue

                # We should only get this when an unhandled close code happens,
                # such as a clean disconnect (1000) or a bad state (bad token, no sharding, etc)
                # sometimes, discord sends us 1000 for unknown reasons so we should reconnect
                # regardless and rely on is_closed instead
                if isinstance(exc, ConnectionClosed):
                    if exc.code == 4014:
                        raise PrivilegedIntentsRequired(exc.shard_id) from None
                    if exc.code != 1000:
                        await self.close()
                        raise

                retry = backoff.delay()
                log.exception("Attempting a reconnect in %.2fs", retry)
                await asyncio.sleep(retry)
                # Always try to RESUME the connection
                # If the connection is not RESUME-able then the gateway will invalidate the session.
                # This is apparently what the official Discord client does.
                ws_params.update(sequence=self.ws.sequence, resume=True, session=self.ws.session_id)

    async def close(self):
        """|coro|

        Closes the connection to Discord.
        """
        if self._closed:
            return

        #await self.http.close()
        self._closed = True

        if self.ws is not None and self.ws.open:
            await self.ws.close(code=1000)

        #self._ready.clear()

    def clear(self):
        """Clears the internal state of the bot.

        After this, the bot can be considered "re-opened", i.e. :meth:`is_closed`
        and :meth:`is_ready` both return ``False`` along with the bot's internal
        cache cleared.
        """
        self._closed = False
        self._ready.clear()
        self.http.recreate()

    async def start(self, *args, **kwargs):
        """|coro|

        A shorthand coroutine for :meth:`login` + :meth:`connect`.

        Raises
        -------
        TypeError
            An unexpected keyword argument was received.
        """
        bot = kwargs.pop('bot', True)
        reconnect = kwargs.pop('reconnect', True)

        if kwargs:
            raise TypeError("unexpected keyword argument(s) %s" % list(kwargs.keys()))

        await self.login(*args, bot=bot)
        await self.connect(reconnect=reconnect)

    def run(self, *args, **kwargs):
        """A blocking call that abstracts away the event loop
        initialisation from you.

        If you want more control over the event loop then this
        function should not be used. Use :meth:`start` coroutine
        or :meth:`connect` + :meth:`login`.

        Roughly Equivalent to: ::

            try:
                loop.run_until_complete(start(*args, **kwargs))
            except KeyboardInterrupt:
                loop.run_until_complete(close())
                # cancel all tasks lingering
            finally:
                loop.close()

        .. warning::

            This function must be the last function to call due to the fact that it
            is blocking. That means that registration of events or anything being
            called after this function call will not execute until it returns.
        """
        loop = self.loop

        try:
            loop.add_signal_handler(signal.SIGINT, lambda: loop.stop())
            loop.add_signal_handler(signal.SIGTERM, lambda: loop.stop())
        except NotImplementedError:
            pass

        async def runner():
            try:
                await self.start(*args, **kwargs)
            finally:
                if not self.is_closed():
                    await self.close()

        def stop_loop_on_completion(f):
            loop.stop()

        future = asyncio.ensure_future(runner(), loop=loop)
        future.add_done_callback(stop_loop_on_completion)
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            log.info('Received signal to terminate bot and event loop.')
        finally:
            future.remove_done_callback(stop_loop_on_completion)
            log.info('Cleaning up tasks.')
            _cleanup_loop(loop)

        if not future.cancelled():
            try:
                return future.result()
            except KeyboardInterrupt:
                # I am unsure why this gets raised here but suppress it anyway
                return None

    # internals

    async def _call_before_identify_hook(self, shard_id, *, initial=False):
        # This hook is an internal hook that actually calls the public one.
        # It allows the library to have its own hook without stepping on the
        # toes of those who need to override their own hook.
        await self.before_identify_hook(shard_id, initial=initial)

    async def before_identify_hook(self, shard_id, *, initial=False):
        """|coro|

        A hook that is called before IDENTIFYing a session. This is useful
        if you wish to have more control over the synchronization of multiple
        IDENTIFYing clients.

        The default implementation sleeps for 5 seconds.

        .. versionadded:: 1.4

        Parameters
        ------------
        shard_id: :class:`int`
            The shard ID that requested being IDENTIFY'd
        initial: :class:`bool`
            Whether this IDENTIFY is the first initial IDENTIFY.
        """

        if not initial:
            await asyncio.sleep(5.0)

    def _get_websocket(self, guild_id=None, *, shard_id=None):
        return self.ws

    def _get_state(self, **options):
        return ConnectionState(dispatch=self.dispatch, handlers=self._handlers,
                               hooks=self._hooks, syncer=self._syncer, http=self.http, loop=self.loop, **options)

    async def _syncer(self, guilds):
        await self.ws.request_sync(guilds)

    def _handle_ready(self):
        self._ready.set()

    def dispatch(self, event, *args, **kwargs):
        method = 'on_' + event

        listeners = self._listeners.get(event)
        if listeners:
            removed = []
            for i, (future, condition) in enumerate(listeners):
                if future.cancelled():
                    removed.append(i)
                    continue

                try:
                    result = condition(*args)
                except Exception as exc:
                    future.set_exception(exc)
                    removed.append(i)
                else:
                    if result:
                        if len(args) == 0:
                            future.set_result(None)
                        elif len(args) == 1:
                            future.set_result(args[0])
                        else:
                            future.set_result(args)
                        removed.append(i)

            if len(removed) == len(listeners):
                self._listeners.pop(event)
            else:
                for idx in reversed(removed):
                    del listeners[idx]

        try:
            coro = getattr(self, method)
        except AttributeError:
            pass
        else:
            self._schedule_event(coro, method, *args, **kwargs)

    @property
    def latency(self):
        """:class:`float`: Measures latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds.

        This could be referred to as the Discord WebSocket protocol latency.
        """
        ws = self.ws
        return float('nan') if not ws else ws.latency

    def is_ws_ratelimited(self):
        """:class:`bool`: Whether the websocket is currently rate limited.

        This can be useful to know when deciding whether you should query members
        using HTTP or via the gateway.

        .. versionadded:: 1.6
        """
        if self.ws:
            return self.ws.is_ratelimited()
        return False

    @property
    def user(self):
        """Optional[:class:`.ClientUser`]: Represents the connected client. ``None`` if not logged in."""
        return self._connection.user

    @property
    def guilds(self):
        """List[:class:`.Guild`]: The guilds that the connected client is a member of."""
        return self._connection.guilds

    @property
    def emojis(self):
        """List[:class:`.Emoji`]: The emojis that the connected client has."""
        return self._connection.emojis

    @property
    def cached_messages(self):
        """Sequence[:class:`.Message`]: Read-only list of messages the connected client has cached.

        .. versionadded:: 1.1
        """
        return utils.SequenceProxy(self._connection._messages or [])

    @property
    def private_channels(self):
        """List[:class:`.abc.PrivateChannel`]: The private channels that the connected client is participating on.

        .. note::

            This returns only up to 128 most recent private channels due to an internal working
            on how Discord deals with private channels.
        """
        return self._connection.private_channels

    def is_ready(self):
        """:class:`bool`: Specifies if the client's internal cache is ready for use."""
        return self._ready.is_set()

    async def _run_event(self, coro, event_name, *args, **kwargs):
        try:
            await coro(*args, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception:
            try:
                await self.on_error(event_name, *args, **kwargs)
            except asyncio.CancelledError:
                pass

    # properties

    def is_closed(self):
        """:class:`bool`: Indicates if the websocket connection is closed."""
        return self._closed

    def synchronize_commands(self, name: Optional[str] = None) -> None:
        # TODO: Doctype what this does.
        commands = self.loop.run_until_complete(self.http.get_application_command(self.me.id))
        change: list = []

        for command in commands:
            _command: Optional[Item] = cache.interactions.get(command["id"])
            if _command:
                if ApplicationCommand(**command) == _command:
                    log.warning(f"Detected change to command ID {command.id}.")
                    change.append(command)
            else:
                cache.interactions.add(Item(command["id"], ApplicationCommand(**command)))

        for command in change:
            log.debug(f"Updated command {command.id}.")
            self.http.edit_application_command(
                application_id=self.me.id,
                data=command["data"],
                command_id=command["id"],
                guild_id=command.get("guild_id"),
            )
            cache.interactions.add(Item(command["id"], ApplicationCommand(**command)))

    def event(self, coro: Coroutine) -> Callable[..., Any]:
        """
        A decorator for listening to dispatched events from the
        gateway.

        :return: typing.Callable[..., typing.Any]
        """
        #self.ws.dispatch.register(
        #    coro, name=coro.__name__ if coro.__name__.startswith("on") else "on_interaction_create"
        #)
        #return coro

    def command(
        self,
        *,
        type: Optional[Union[str, int, ApplicationCommandType]] = ApplicationCommandType.CHAT_INPUT,
        name: Optional[str] = None,
        description: Optional[str] = None,
        scope: Optional[Union[int, Guild, List[int], List[Guild]]] = None,
        options: Optional[List[Option]] = None,
        default_permission: Optional[bool] = None
        # permissions: Optional[List[Permission]] = None,
    ) -> Callable[..., Any]:
        """
        A decorator for registering an application command to the Discord API,
        as well as being able to listen for ``INTERACTION_CREATE`` dispatched
        gateway events.

        :param type: The type of application command. Defaults to :meth:`interactions.enums.ApplicationCommandType.CHAT_INPUT` or ``1``.
        :type type: typing.Optional[typing.Union[str, int, interactions.enums.ApplicationCommandType]]
        :param name: The name of the application command. This *is* required but kept optional to follow kwarg rules.
        :type name: typing.Optional[str]
        :param description: The description of the application command. This should be left blank if you are not using ``CHAT_INPUT``.
        :type description: typing.Optional[str]
        :param scope: The "scope"/applicable guilds the application command applies to.
        :type scope: typing.Optional[typing.Union[int, interactions.api.models.guild.Guild, typing.List[int], typing.List[interactions.api.models.guild.Guild]]]
        :param options: The "arguments"/options of an application command. This should bel eft blank if you are not using ``CHAT_INPUT``.
        :type options: typing.Optional[typing.List[interactions.models.command.Option]]
        :param default_permission: The default permission of accessibility for the application command. Defaults to ``True``.
        :type default_permission: typing.Optional[bool]
        :return: typing.Callable[..., typing.Any]
        """
        if not name:
            raise Exception("Command must have a name!")

        if name and (type != 3 and not description):
            raise Exception("Chat-input commands must have a description!")

        def decorator(coro: Coroutine) -> Any:
            if "ctx" not in coro.__code__.co_varnames:
                raise InteractionException(11)

            _description: str = "" if description is None else description
            _options: list = [] if options is None else options
            _default_permission: bool = True if default_permission is None else default_permission
            # _permissions: list = [] if permissions is None else permissions
            _scope: list = []

            if isinstance(scope, list):
                if all(isinstance(x, Guild) for x in scope):
                    _scope.append(guild.id for guild in scope)
                elif all(isinstance(x, int) for x in scope):
                    _scope.append(guild for guild in scope)
            else:
                _scope.append(scope)

            for guild in _scope:
                payload: ApplicationCommand = ApplicationCommand(
                    type=type.value if isinstance(type, ApplicationCommandType) else type,
                    name=name,
                    description=_description,
                    options=_options,
                    default_permission=_default_permission,
                )

                request = self.loop.run_until_complete(
                    self.http.create_application_command(
                        self.me.id, data=payload._json, guild_id=guild
                    )
                )

                if request.get("code"):
                    raise JSONException(request["code"])  # TODO: work on this pls

                for interaction in cache.interactions.values:
                    if interaction.values[interaction].value.name == name:
                        self.synchronize_commands(name)
                        # TODO: make a call to our internal sync method instead of an exception.
                    else:
                        cache.interactions.add(Item(id=request["application_id"], value=payload))

            return self.event(coro)

        return decorator

    async def raw_socket_create(self, data: dict) -> None:
        # TODO: doctype what this does
        return data

    async def raw_guild_create(self, guild) -> None:
        """
        This is an internal function that caches the guild creates on ready.
        :param guild: Guild object.
        :return: None.
        """
        cache.guilds.add(Item(id=guild.id, value=guild))
