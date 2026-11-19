class InvalidMultiplier(ValueError):
    """A raw instrument unit must correspond to positive integer micrometres."""


def require_micrometre_multiplier(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise InvalidMultiplier('micrometre multiplier must be a positive integer')
    return value
