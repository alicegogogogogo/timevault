class TimeVaultError(Exception):
    code = "internal_error"
    status = 500


class ValidationError(TimeVaultError, ValueError):
    """A request that violates the public constraints of an entry point.

    It is also a :class:`ValueError`, so a caller that follows the documented
    contract of the read APIs — bad coordinates are value errors — can catch
    the built-in category without depending on this concrete subclass.  Every
    existing ``except ValidationError`` and ``except TimeVaultError`` clause
    behaves exactly as before.
    """

    code = "validation_error"
    status = 400


class NotFoundError(TimeVaultError):
    code = "not_found"
    status = 404


class ConflictError(TimeVaultError):
    code = "conflict"
    status = 409
