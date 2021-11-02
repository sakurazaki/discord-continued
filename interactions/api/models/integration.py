from .misc import DictSerializerMixin

class Message(DictSerializerMixin):
    """
    Integration Model, unfinished.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
