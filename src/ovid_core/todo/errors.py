from ovid_core.errors import OvidCoreError, PersistenceError


class TodoError(OvidCoreError):
    pass


class TodoOperationError(TodoError):
    pass


class TodoTargetNotFoundError(TodoOperationError):
    pass


class TodoDuplicateError(TodoOperationError):
    pass


class TodoLimitError(TodoOperationError):
    pass


class TodoConflictError(TodoError):
    pass


class TodoStoreError(PersistenceError):
    pass
