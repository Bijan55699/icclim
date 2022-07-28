from __future__ import annotations

from icclim.icclim_exceptions import InvalidIcclimArgumentError


class Registry:
    """This class is a fancy enum to easily store and find constant items of
    similar type.

    It acts as a namespace so there is no need to instantiate it or it's subclasses.
    """

    _item_class: type

    def __init__(self):
        raise NotImplementedError("Don't instantiate Registry, use its class methods.")

    @classmethod
    def lookup(cls, query: _item_class | str, no_error: bool = False) -> _item_class:
        if isinstance(query, cls._item_class):
            return query
        q = query.upper()
        for key, item in cls.catalog().items():
            if q == key.upper() or q in cls.get_item_aliases(item):
                return item
        if no_error:
            return None
        raise InvalidIcclimArgumentError(
            f"Unknown {cls._item_class.__qualname__}: '{query}'. "
            f"Use one of {cls.all_aliases()}."
        )

    @classmethod
    def all_aliases(cls) -> list[_item_class]:
        return list(map(cls.get_item_aliases, list(cls.catalog().values())))

    @staticmethod
    def get_item_aliases(item: _item_class) -> list[str]:
        return [item.name.upper()]

    @classmethod
    def catalog(cls) -> dict[str, _item_class]:
        return {k: v for k, v in cls.__dict__.items() if isinstance(v, cls._item_class)}

    @classmethod
    def values(cls) -> list[_item_class]:
        return [v for k, v in cls.__dict__.items() if isinstance(v, cls._item_class)]
