# -*- coding: utf-8 -*-

from .flags import PublicUserFlags
from .utils import snowflake_time, _bytes_to_base64_data, parse_time
from .enums import DefaultAvatar, RelationshipType, UserFlags, HypeSquadHouse, PremiumType, try_enum
from ..exceptions import ClientException
from .colour import Colour
from .asset import Asset

__all__ = (
    'Profile',
    'BaseUser',
    'ClientUser',
    'User'
)

class Profile:
    __slots__ = ()

    @property
    def nitro(self):
        return self.premium_since is not None

    premium = nitro

    def _has_flag(self, o):
        v = o.value
        return (self.flags & v) == v

    @property
    def staff(self):
        return self._has_flag(UserFlags.staff)

    @property
    def partner(self):
        return self._has_flag(UserFlags.partner)

    @property
    def bug_hunter(self):
        return self._has_flag(UserFlags.bug_hunter)

    @property
    def early_supporter(self):
        return self._has_flag(UserFlags.early_supporter)

    @property
    def hypesquad(self):
        return self._has_flag(UserFlags.hypesquad)

    @property
    def hypesquad_houses(self):
        flags = (UserFlags.hypesquad_bravery, UserFlags.hypesquad_brilliance, UserFlags.hypesquad_balance)
        return [house for house, flag in zip(HypeSquadHouse, flags) if self._has_flag(flag)]

    @property
    def team_user(self):
        return self._has_flag(UserFlags.team_user)

    @property
    def system(self):
        return self._has_flag(UserFlags.system)

class BaseUser:
    __slots__ = ('name', 'id', 'discriminator', 'avatar', 'bot', 'system', '_public_flags', '_client')

    def __init__(self, *, client, data):
        self._client = client
        self._update(data)

    def __str__(self):
        return '{0.name}#{0.discriminator}'.format(self)

    def __eq__(self, other):
        return isinstance(other, BaseUser) and other.id == self.id

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return self.id >> 22

    def _update(self, data):
        self.name = data['username']
        self.id = int(data['id'])
        self.discriminator = data['discriminator']
        self.avatar = data['avatar']
        self._public_flags = data.get('public_flags', 0)
        self.bot = data.get('bot', False)
        self.system = data.get('system', False)

    @classmethod
    def _copy(cls, user):
        self = cls.__new__(cls) # bypass __init__

        self.name = user.name
        self.id = user.id
        self.discriminator = user.discriminator
        self.avatar = user.avatar
        self.bot = user.bot
        self._client = user._client
        self._public_flags = user._public_flags

        return self

    def _to_minimal_user_json(self):
        return {
            'username': self.name,
            'id': self.id,
            'avatar': self.avatar,
            'discriminator': self.discriminator,
            'bot': self.bot,
        }

    @property
    def public_flags(self):
        """:class:`PublicUserFlags`: The publicly available flags the user has."""
        return PublicUserFlags._from_value(self._public_flags)

    @property
    def avatar_url(self):
        """:class:`Asset`: Returns an :class:`Asset` for the avatar the user has.

        If the user does not have a traditional avatar, an asset for
        the default avatar is returned instead.

        This is equivalent to calling :meth:`avatar_url_as` with
        the default parameters (i.e. webp/gif detection and a size of 1024).
        """
        return self.avatar_url_as(format=None, size=1024)

    def is_avatar_animated(self):
        """:class:`bool`: Indicates if the user has an animated avatar."""
        return bool(self.avatar and self.avatar.startswith('a_'))

    def avatar_url_as(self, *, format=None, static_format='webp', size=1024):
        """Returns an :class:`Asset` for the avatar the user has.

        If the user does not have a traditional avatar, an asset for
        the default avatar is returned instead.

        The format must be one of 'webp', 'jpeg', 'jpg', 'png' or 'gif', and
        'gif' is only valid for animated avatars. The size must be a power of 2
        between 16 and 4096.

        Parameters
        -----------
        format: Optional[:class:`str`]
            The format to attempt to convert the avatar to.
            If the format is ``None``, then it is automatically
            detected into either 'gif' or static_format depending on the
            avatar being animated or not.
        static_format: Optional[:class:`str`]
            Format to attempt to convert only non-animated avatars to.
            Defaults to 'webp'
        size: :class:`int`
            The size of the image to display.

        Raises
        ------
        InvalidArgument
            Bad image format passed to ``format`` or ``static_format``, or
            invalid ``size``.

        Returns
        --------
        :class:`Asset`
            The resulting CDN asset.
        """
        return Asset._from_avatar(self._client, self, format=format, static_format=static_format, size=size)

    @property
    def default_avatar(self):
        """:class:`DefaultAvatar`: Returns the default avatar for a given user. This is calculated by the user's discriminator."""
        return try_enum(DefaultAvatar, int(self.discriminator) % len(DefaultAvatar))

    @property
    def default_avatar_url(self):
        """:class:`Asset`: Returns a URL for a user's default avatar."""
        return Asset(self._client, '/embed/avatars/{}.png'.format(self.default_avatar.value))

    @property
    def colour(self):
        """:class:`Colour`: A property that returns a colour denoting the rendered colour
        for the user. This always returns :meth:`Colour.default`.

        There is an alias for this named :attr:`color`.
        """
        return Colour.default()

    @property
    def color(self):
        """:class:`Colour`: A property that returns a color denoting the rendered color
        for the user. This always returns :meth:`Colour.default`.

        There is an alias for this named :attr:`colour`.
        """
        return self.colour

    @property
    def mention(self):
        """:class:`str`: Returns a string that allows you to mention the given user."""
        return '<@{0.id}>'.format(self)

    def permissions_in(self, channel):
        """An alias for :meth:`abc.GuildChannel.permissions_for`.

        Basically equivalent to:

        .. code-block:: python3

            channel.permissions_for(self)

        Parameters
        -----------
        channel: :class:`abc.GuildChannel`
            The channel to check your permissions for.
        """
        return channel.permissions_for(self)

    @property
    def created_at(self):
        """:class:`datetime.datetime`: Returns the user's creation time in UTC.

        This is when the user's Discord account was created."""
        return snowflake_time(self.id)

    @property
    def display_name(self):
        """:class:`str`: Returns the user's display name.

        For regular users this is just their username, but
        if they have a guild specific nickname then that
        is returned instead.
        """
        return self.name

    def mentioned_in(self, message):
        """Checks if the user is mentioned in the specified message.

        Parameters
        -----------
        message: :class:`Message`
            The message to check if you're mentioned in.

        Returns
        -------
        :class:`bool`
            Indicates if the user is mentioned in the message.
        """

        if message.mention_everyone:
            return True

        return any(user.id == self.id for user in message.mentions)

class ClientUser(BaseUser):
    """Represents your Discord user.

    .. container:: operations

        .. describe:: x == y

            Checks if two users are equal.

        .. describe:: x != y

            Checks if two users are not equal.

        .. describe:: hash(x)

            Return the user's hash.

        .. describe:: str(x)

            Returns the user's name with discriminator.

    Attributes
    -----------
    name: :class:`str`
        The user's username.
    id: :class:`int`
        The user's unique ID.
    discriminator: :class:`str`
        The user's discriminator. This is given when the username has conflicts.
    avatar: Optional[:class:`str`]
        The avatar hash the user has. Could be ``None``.
    bot: :class:`bool`
        Specifies if the user is a bot account.
    system: :class:`bool`
        Specifies if the user is a system user (i.e. represents Discord officially).

    verified: :class:`bool`
        Specifies if the user's email is verified.
    email: Optional[:class:`str`]
        The email the user used when registering.
    locale: Optional[:class:`str`]
        The IETF language tag used to identify the language the user is using.
    mfa_enabled: :class:`bool`
        Specifies if the user has MFA turned on and working.
    premium: :class:`bool`
        Specifies if the user is a premium user (e.g. has Discord Nitro).
    premium_type: Optional[:class:`PremiumType`]
        Specifies the type of premium a user has (e.g. Nitro or Nitro Classic). Could be None if the user is not premium.
    """
    __slots__ = BaseUser.__slots__ + \
                ('email', 'locale', '_flags', 'verified', 'mfa_enabled',
                 'premium', 'premium_type', '_relationships', '__weakref__')

    def __init__(self, *, client, data):
        super().__init__(client=client, data=data)
        self._relationships = {}

    def __repr__(self):
        return '<ClientUser id={0.id} name={0.name!r} discriminator={0.discriminator!r}' \
               ' bot={0.bot} verified={0.verified} mfa_enabled={0.mfa_enabled}>'.format(self)

    def _update(self, data):
        super()._update(data)
        # There's actually an Optional[str] phone field as well but I won't use it
        self.verified = data.get('verified', False)
        self.email = data.get('email')
        self.locale = data.get('locale')
        self._flags = data.get('flags', 0)
        self.mfa_enabled = data.get('mfa_enabled', False)
        self.premium = data.get('premium', False)
        self.premium_type = try_enum(PremiumType, data.get('premium_type', None))

    @property
    def relationships(self):
        """List[:class:`User`]: Returns all the relationships that the user has.

        .. deprecated:: 1.7

        .. note::

            This can only be used by non-bot accounts.
        """
        return list(self._relationships.values())

    @property
    def friends(self):
        r"""List[:class:`User`]: Returns all the users that the user is friends with.

        .. deprecated:: 1.7

        .. note::

            This can only be used by non-bot accounts.
        """
        return [r.user for r in self._relationships.values() if r.type is RelationshipType.friend]

    @property
    def blocked(self):
        r"""List[:class:`User`]: Returns all the users that the user has blocked.

        .. deprecated:: 1.7

        .. note::

            This can only be used by non-bot accounts.
        """
        return [r.user for r in self._relationships.values() if r.type is RelationshipType.blocked]

    async def edit(self, **fields):
        """|coro|

        Edits the current profile of the client.

        If a bot account is used then a password field is optional,
        otherwise it is required.

        .. warning::

            The user account-only fields are deprecated.

        .. note::

            To upload an avatar, a :term:`py:bytes-like object` must be passed in that
            represents the image being uploaded. If this is done through a file
            then the file must be opened via ``open('some_filename', 'rb')`` and
            the :term:`py:bytes-like object` is given through the use of ``fp.read()``.

            The only image formats supported for uploading is JPEG and PNG.

        Parameters
        -----------
        password: :class:`str`
            The current password for the client's account.
            Only applicable to user accounts.
        new_password: :class:`str`
            The new password you wish to change to.
            Only applicable to user accounts.
        email: :class:`str`
            The new email you wish to change to.
            Only applicable to user accounts.
        house: Optional[:class:`HypeSquadHouse`]
            The hypesquad house you wish to change to.
            Could be ``None`` to leave the current house.
            Only applicable to user accounts.
        username: :class:`str`
            The new username you wish to change to.
        avatar: :class:`bytes`
            A :term:`py:bytes-like object` representing the image to upload.
            Could be ``None`` to denote no avatar.

        Raises
        ------
        HTTPException
            Editing your profile failed.
        InvalidArgument
            Wrong image format passed for ``avatar``.
        ClientException
            Password is required for non-bot accounts.
            House field was not a HypeSquadHouse.
        """

        try:
            avatar_bytes = fields['avatar']
        except KeyError:
            avatar = self.avatar
        else:
            if avatar_bytes is not None:
                avatar = _bytes_to_base64_data(avatar_bytes)
            else:
                avatar = None

        not_bot_account = not self.bot
        password = fields.get('password')
        if not_bot_account and password is None:
            raise ClientException('Password is required for non-bot accounts.')

        args = {
            'password': password,
            'username': fields.get('username', self.name),
            'avatar': avatar
        }

        if not_bot_account:
            args['email'] = fields.get('email', self.email)

            if 'new_password' in fields:
                args['new_password'] = fields['new_password']

        http = self._client.http

        if 'house' in fields:
            house = fields['house']
            if house is None:
                await http.leave_hypesquad_house()
            elif not isinstance(house, HypeSquadHouse):
                raise ClientException('`house` parameter was not a HypeSquadHouse')
            else:
                value = house.value

            await http.change_hypesquad_house(value)

        data = await http.edit_profile(**args)
        if not_bot_account:
            self.email = data['email']
            try:
                http._token(data['token'], bot=False)
            except KeyError:
                pass

        self._update(data)

class User(BaseUser):
    """Represents a Discord user.

    .. container:: operations

        .. describe:: x == y

            Checks if two users are equal.

        .. describe:: x != y

            Checks if two users are not equal.

        .. describe:: hash(x)

            Return the user's hash.

        .. describe:: str(x)

            Returns the user's name with discriminator.

    Attributes
    -----------
    name: :class:`str`
        The user's username.
    id: :class:`int`
        The user's unique ID.
    discriminator: :class:`str`
        The user's discriminator. This is given when the username has conflicts.
    avatar: Optional[:class:`str`]
        The avatar hash the user has. Could be None.
    bot: :class:`bool`
        Specifies if the user is a bot account.
    system: :class:`bool`
        Specifies if the user is a system user (i.e. represents Discord officially).
    """

    __slots__ = BaseUser.__slots__ + ('__weakref__',)

    def __repr__(self):
        return '<User id={0.id} name={0.name!r} discriminator={0.discriminator!r} bot={0.bot}>'.format(self)

    async def _get_channel(self):
        ch = await self.create_dm()
        return ch

    @property
    def dm_channel(self):
        """Optional[:class:`DMChannel`]: Returns the channel associated with this user if it exists.

        If this returns ``None``, you can create a DM channel by calling the
        :meth:`create_dm` coroutine function.
        """
        return self._client.create_dm(self.id)

    @property
    def mutual_guilds(self):
        """List[:class:`Guild`]: The guilds that the user shares with the client.

        .. note::

            This will only return mutual guilds within the client's internal cache.

        .. versionadded:: 1.7
        """
        return [guild for guild in self._client._guilds.values() if guild.get_member(self.id)]

    async def create_dm(self):
        """|coro|

        Creates a :class:`DMChannel` with this user.

        This should be rarely called, as this is done transparently for most
        people.

        Returns
        -------
        :class:`.DMChannel`
            The channel that was created.
        """
        found = self.dm_channel
        if found is not None:
            return found

        state = self._client
        data = await state.http.create_dm(self.id)
        return data
