from .misc import DictSerializerMixin

class Gift(DictSerializerMixin):
    """
    Gift Model, unfinished.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
