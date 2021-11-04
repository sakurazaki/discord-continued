# -*- coding: utf-8 -*-

from ..exceptions import ClientException

from .flags import PublicUserFlags
from .utils import snowflake_time, _bytes_to_base64_data, parse_time
from .enums import DefaultAvatar, RelationshipType, UserFlags, HypeSquadHouse, PremiumType, try_enum
from .colour import Colour
from .user import User
from .asset import Asset
from .member import Member

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

        for key, value in data.items():
            setattr(self, key, value)