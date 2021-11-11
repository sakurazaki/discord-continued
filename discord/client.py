import asyncio
import signal
import aiohttp
from logging import Logger, basicConfig, getLogger
from typing import Any, Callable, Coroutine, List, Optional, Union, Dict

from .api.cache import Cache, Item
from .api.exceptions import *
from .api.gateway import DiscordWebSocket, ReconnectWebSocket
from .api.http import HTTPClient
from .backoff import ExponentialBackoff
from .enums import ApplicationCommandType
from .models import Intents
from .models import ClientUser, Guild
from .models import ApplicationCommand, Option
from .exceptions import *

from .context import Context, InteractionContext

from . import __logger__

__all__ = (
    'Client',
)

# TODO: Find a better way to call on the cache
cache = Cache()

basicConfig(level=__logger__)
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

class _ClientEventTask(asyncio.Task):
    def __init__(self, original_coro, event_name, coro, *, loop):
        super().__init__(coro, loop=loop)
        self.__event_name = event_name
        self.__original_coro = original_coro

    def __repr__(self):
        info = [
            ('state', self._state.lower()),
            ('event', self.__event_name),
            ('coro', repr(self.__original_coro)),
        ]
        if self._exception is not None:
            info.append(('exception', repr(self._exception)))
        return '<ClientEventTask {}>'.format(' '.join('%s=%s' % t for t in info))

class Client:
    """
    A class representing the client connection to Discord's gateway and API via. WebSocket and HTTP.

    :ivar asyncio.AbstractEventLoop loop: The main overall asynchronous coroutine loop in effect.
    :ivar discord.models.Intents intents: Intents to apply when logging.
    :ivar str proxy: Proxy for http library.
    :ivar str proxy_auth: Auth to login into proxy.
    :ivar bool unsync_clock: Assume unsynced clock.
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

        self.intents = options.pop('intents', Intents.all())

        self._handlers = {
            'ready': self._handle_ready
        }

        self._hooks = {
            'before_identify': self._call_before_identify_hook
        }

        self._application_commands: Dict[str, Dict] = {}

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
        self.user = await self.http.static_login(token.strip(), bot=bot)

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

    async def get_self_info(self):
        """|coro|

        After connecting to client and socket fill information of user.

        """

        # Fill user info
        if(self.user):
            self.user = ClientUser(client=self, data=self.user)
        # Get guilds too
        self.guilds = list()
        temp_guilds = await self.http.get_self_guilds()
        for guild in temp_guilds:
            full_guild = await self.http.get_guild(guild['id'])
            self.guilds.append(Guild(client=self, data=full_guild))
            await asyncio.sleep(1) # wait just a second to make sure we dont destroy the request rate limit per second
            # this waiting may be specially bad if a bot is in more than a few servers and if so I suggest
            # moving this functionality to a separate task where to build the internal cache.

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
        await self.get_self_info()
        self.dispatch('ready')
        # .connect locks the execution, so anything you gotta do, before this line
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

    async def _syncer(self, guilds):
        await self.ws.request_sync(guilds)

    def _handle_ready(self, event, data: dict = None):
        self._ready.set()
        log.debug(f"Im Handling ready and setting up {self._application_commands}")
        # Dispatch all pending application_commands
        for name, data in self._application_commands.items():
            self._setup_application_command(name, self.user.id, data)
        asyncio.create_task(self.synchronize_commands())

    def dispatch_raw(self, event, *args, **kwargs):
        method = 'on_' + event

        try:
            coro = getattr(self, method)
        except AttributeError:
            pass
        else:
            self._schedule_event(coro, method, app_context)

    def dispatch(self, event, data: dict = None):
        method = 'on_' + event

        app_context = None
        if data:
            app_context = Context(**data)

        if event in self._handlers:
            self._handlers[event](app_context)

        listeners = self._listeners.get(event)
        if listeners:
            removed = []
            for i, (future, condition) in enumerate(listeners):
                if future.cancelled():
                    removed.append(i)
                    continue

                try:
                    result = condition(app_context)
                except Exception as exc:
                    future.set_exception(exc)
                    removed.append(i)
                else:
                    if result:
                        if app_context:
                            future.set_result(app_context)
                        else:
                            future.set_result(None)
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
            self._schedule_event(coro, method, app_context)

    def dispatch_interaction(self, data):
        app_context = InteractionContext(**data)

        coro = getattr(self, f"{app_context.data['name']}_{app_context.data['type']}", None)
        if coro:
            asyncio.create_task(coro(app_context))

    def _schedule_event(self, coro, event_name, app_context):
        wrapped = self._run_event(coro, event_name, app_context)
        # Schedules the task
        return _ClientEventTask(original_coro=coro, event_name=event_name, coro=wrapped, loop=self.loop)

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

    def is_ready(self):
        """:class:`bool`: Specifies if the client's internal cache is ready for use."""
        return self._ready.is_set()

    async def _run_event(self, coro, event_name, app_context):
        try:
            await coro(app_context)
        except asyncio.CancelledError:
            pass
        except Exception:
            try:
                await self.on_error(event_name, app_context)
            except asyncio.CancelledError:
                pass

    # properties

    def is_closed(self):
        """:class:`bool`: Indicates if the websocket connection is closed."""
        return self._closed

    async def synchronize_commands(self, name: Optional[str] = None) -> None:
        await asyncio.sleep(2) # we're getting rate limited here so wait a bit, we're in no rush
        guilds = await self.http.get_self_guilds()

        for guild in guilds:
            await asyncio.sleep(2)
            commands = await self.http.get_application_command(self.user.id, guild['id'])
            
            change: list = []

            for command in commands:
                _command: Optional[Item] = getattr(self, f"{command['name']}_{command['type']}", None)
                return
                if _command:
                    change.append(_command)
                else:
                    await asyncio.sleep(3)
                    await self.http.delete_application_command(
                        application_id=self.user.id,
                        command_id=command["id"],
                        guild_id=command.get("guild_id"),
                    )

            for command in change:
                log.debug(f"Updated command {command['id']}.")
                await asyncio.sleep(3)
                self.http.edit_application_command(
                    application_id=self.user.id,
                    data=command["data"],
                    command_id=command["id"],
                    guild_id=command.get("guild_id"),
                )

    def event(self, coro: Coroutine) -> Callable[..., Any]:
        """A decorator that registers an event to listen to.

        You can find more info about the events on the :ref:`documentation below <discord-api-events>`.

        The events must be a :ref:`coroutine <coroutine>`, if not, :exc:`TypeError` is raised.

        Example
        ---------

        .. code-block:: python3

            @client.event
            async def on_ready():
                print('Ready!')

        Raises
        --------
        TypeError
            The coroutine passed is not actually a coroutine.
        """

        if not asyncio.iscoroutinefunction(coro):
            raise TypeError('event registered must be a coroutine function')

        setattr(self, coro.__name__, coro)
        log.debug('%s has successfully been registered as an event', coro.__name__)
        return coro

    def command(
        self,
        *,
        type: Optional[Union[str, int, ApplicationCommandType]] = ApplicationCommandType.CHAT_INPUT,
        name: Optional[str] = None,
        description: Optional[str] = None,
        scope: Optional[Union[int, List[int]]] = None,
        options: Optional[List[Option]] = None,
        default_permission: Optional[bool] = None,
        permissions: Optional[List[str]] = None
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

        def decorator(coro: Coroutine):
            if not asyncio.iscoroutinefunction(coro):
                raise TypeError('event registered must be a coroutine function')

            # Store the function in the bot
            setattr(self, f"{name}_{type}", coro)
            application_data = {'type': type, 'name': name, 'description': description, 'scope': scope, 
                    'options': options, 'default_permission': default_permission, 'permissions': permissions}

            # We need to verify whether the bot is logged in or not.
            if self.is_ready():
                # Just setup the command
                _setup_application_command(name, self.user.id, application_data)

            else:
                # We delegate the command to after the bot is logged in
                self._application_commands[f"{name}_{type}"] = application_data
                
            log.debug('%s has successfully been queued as an application command', coro.__name__)

            return coro

        return decorator

    def _setup_application_command(self, name: str, application_id: int, application_data: dict):
        _type: int = application_data["type"].value if isinstance(application_data["type"], ApplicationCommandType) else application_data["type"]
        _description: str = "" if application_data.get("description") is None else application_data["description"]
        _options: list = [] if application_data.get("options") is None else application_data["options"]
        _default_permission: bool = True if application_data.get("default_permission") is None else application_data["default_permission"]
        _permissions: list = [] if application_data['permissions'] is None else application_data['permissions']
        _scope: list = []

        if isinstance(application_data.get("description"), list):
            if all(isinstance(x, int) for x in scope):
                _scope.append(guild for guild in scope)
        else:
            _scope.append(application_data.get("scope"))

        # Double check just so users don't fuck up
        if len(_permissions) > 0:
            _default_permission = False

        for guild in _scope:
            payload: ApplicationCommand = ApplicationCommand(
                type=_type,
                name=application_data['name'],
                description=_description,
                options=_options,
                default_permission=_default_permission,
            )

            asyncio.create_task(self._register_application_command_and_permissions(application_id, guild, payload, _permissions))

    async def _register_application_command_and_permissions(self, application_id, guild, payload, permissions):
        request = await self.http.create_application_command(
            application_id=application_id, data=payload._json, guild_id=guild
        )

        command_id = request['id']

        if len(permissions) > 0:
            request = await self.http.edit_application_command_permissions(
                application_id=application_id, guild_id=guild, command_id=command_id, data=permissions
            )

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

    def wait_for(self, event, *, check=None, timeout=None):
        """|coro|

        Waits for a WebSocket event to be dispatched.

        This could be used to wait for a user to reply to a message,
        or to react to a message, or to edit a message in a self-contained
        way.

        The ``timeout`` parameter is passed onto :func:`asyncio.wait_for`. By default,
        it does not timeout. Note that this does propagate the
        :exc:`asyncio.TimeoutError` for you in case of timeout and is provided for
        ease of use.

        In case the event returns multiple arguments, a :class:`tuple` containing those
        arguments is returned instead. Please check the
        :ref:`documentation <discord-api-events>` for a list of events and their
        parameters.

        This function returns the **first event that meets the requirements**.

        Examples
        ---------

        Waiting for a user reply: ::

            @client.event
            async def on_message(message):
                if message.content.startswith('$greet'):
                    channel = message.channel
                    await channel.send('Say hello!')

                    def check(m):
                        return m.content == 'hello' and m.channel == channel

                    msg = await client.wait_for('message', check=check)
                    await channel.send('Hello {.author}!'.format(msg))

        Waiting for a thumbs up reaction from the message author: ::

            @client.event
            async def on_message(message):
                if message.content.startswith('$thumb'):
                    channel = message.channel
                    await channel.send('Send me that \N{THUMBS UP SIGN} reaction, mate')

                    def check(reaction, user):
                        return user == message.author and str(reaction.emoji) == '\N{THUMBS UP SIGN}'

                    try:
                        reaction, user = await client.wait_for('reaction_add', timeout=60.0, check=check)
                    except asyncio.TimeoutError:
                        await channel.send('\N{THUMBS DOWN SIGN}')
                    else:
                        await channel.send('\N{THUMBS UP SIGN}')


        Parameters
        ------------
        event: :class:`str`
            The event name, similar to the :ref:`event reference <discord-api-events>`,
            but without the ``on_`` prefix, to wait for.
        check: Optional[Callable[..., :class:`bool`]]
            A predicate to check what to wait for. The arguments must meet the
            parameters of the event being waited for.
        timeout: Optional[:class:`float`]
            The number of seconds to wait before timing out and raising
            :exc:`asyncio.TimeoutError`.

        Raises
        -------
        asyncio.TimeoutError
            If a timeout is provided and it was reached.

        Returns
        --------
        Any
            Returns no arguments, a single argument, or a :class:`tuple` of multiple
            arguments that mirrors the parameters passed in the
            :ref:`event reference <discord-api-events>`.
        """

        future = self.loop.create_future()
        if check is None:
            def _check(*args):
                return True
            check = _check

        ev = event.lower()
        try:
            listeners = self._listeners[ev]
        except KeyError:
            listeners = []
            self._listeners[ev] = listeners

        listeners.append((future, check))
        return asyncio.wait_for(future, timeout)