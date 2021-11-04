# -*- coding: utf-8 -*-

class DiscordException(Exception):
    """Base exception class for discord.py

    Ideally speaking, this could be caught to handle any exceptions thrown from this library.
    """
    pass

class NoMoreItems(DiscordException):
    """Exception that is thrown when an async iteration operation has no more
    items."""
    pass

class ClientException(DiscordException):
    """Exception that's thrown when an operation in the :class:`Client` fails.

    These are usually for exceptions that happened due to user input.
    """
    pass

class InvalidData(ClientException):
    """Exception that's raised when the library encounters unknown
    or invalid data from Discord.
    """
    pass

class InvalidArgument(ClientException):
    """Exception that's thrown when an argument to a function
    is invalid some way (e.g. wrong value or wrong type).

    This could be considered the analogous of ``ValueError`` and
    ``TypeError`` except inherited from :exc:`ClientException` and thus
    :exc:`DiscordException`.
    """
    pass

class LoginFailure(ClientException):
    """Exception that's thrown when the :meth:`Client.login` function
    fails to log you in from improper credentials or some other misc.
    failure.
    """
    pass

class ConnectionClosed(ClientException):
    """Exception that's thrown when the gateway connection is
    closed for reasons that could not be handled internally.

    Attributes
    -----------
    code: :class:`int`
        The close code of the websocket.
    reason: :class:`str`
        The reason provided for the closure.
    shard_id: Optional[:class:`int`]
        The shard ID that got closed if applicable.
    """
    def __init__(self, socket, *, shard_id, code=None):
        # This exception is just the same exception except
        # reconfigured to subclass ClientException for users
        self.code = code or socket.close_code
        # aiohttp doesn't seem to consistently provide close reason
        self.reason = ''
        self.shard_id = shard_id
        super().__init__('Shard ID %s WebSocket closed with %s' % (self.shard_id, self.code))

class PrivilegedIntentsRequired(ClientException):
    """Exception that's thrown when the gateway is requesting privileged intents
    but they're not ticked in the developer page yet.

    Go to https://discord.com/developers/applications/ and enable the intents
    that are required. Currently these are as follows:

    - :attr:`Intents.members`
    - :attr:`Intents.presences`

    Attributes
    -----------
    shard_id: Optional[:class:`int`]
        The shard ID that got closed if applicable.
    """

    def __init__(self, shard_id):
        self.shard_id = shard_id
        msg = 'Shard ID %s is requesting privileged intents that have not been explicitly enabled in the ' \
              'developer portal. It is recommended to go to https://discord.com/developers/applications/ ' \
              'and explicitly enable the privileged intents within your application\'s page. If this is not ' \
              'possible, then consider disabling the privileged intents instead.'
        super().__init__(msg % shard_id)
