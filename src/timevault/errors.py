class TimeVaultError(Exception):
    code = "internal_error"
    status = 500


class ValidationError(TimeVaultError):
    code = "validation_error"
    status = 400


class NotFoundError(TimeVaultError):
    code = "not_found"
    status = 404


class ConflictError(TimeVaultError):
    code = "conflict"
    status = 409
