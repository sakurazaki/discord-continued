# -*- coding: utf-8 -*-

from ..exceptions import ClientException

from .flags import PublicUserFlags
from .utils import snowflake_time, _bytes_to_base64_data, parse_time
from .enums import DefaultAvatar, RelationshipType, UserFlags, HypeSquadHouse, PremiumType, try_enum
from .colour import Colour
from .user import User
from .asset import Asset
from .member import Member
from .role import Role

__all__ = (
    'BasicGuild',
    'Guild'
)

class BasicGuild:
    __slots__ = ()

    def __init__(self, *, client, data: dict):
        self._client = client

        for key, value in data.items():
            setattr(self, key, value)


class Guild:

    def __init__(self, *, client, data: dict):
        self._client = client

        # Init stuff from an actual Guild
        if data.get("members"):
            data['members'] = [Member(data=val, guild=data['id'], client=self._client) for val in data['members']]

        if data.get("roles"):
            data['roles'] = [Role(guild=self, data=role, client=self._client) for role in data['roles']]

        for key, value in data.items():
            setattr(self, key, value)

    def __repr__(self) -> str:
        attrs = [f'{key}={value}' for key, value in self.__dict__.items() if (not key.startswith("_") and key != 'emojis')]
        return '<Guild {}>'.format(" ".join(attrs))