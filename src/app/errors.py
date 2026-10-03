''' Error classes shared across layers. Lives at the app root so `processing` can
raise what `jobs` catches without importing upward. '''


class PermanentError(Exception):
    ''' The document will never succeed: wrong type, encrypted, corrupt beyond repair.

    Retrying burns MAX_ATTEMPTS x worker time to reach the same answer, so the worker
    sends these straight to FAILED. Everything else is assumed transient and retried. '''
