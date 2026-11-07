''' Error classes shared across layers. Lives at the app root so `processing` can
raise what the pipeline catches without importing upward. '''


class PermanentError(Exception):
    ''' The document will never succeed: wrong type, encrypted, corrupt beyond repair.

    Retrying would only reach the same answer, so the pipeline stops after one attempt. Everything else is assumed transient and retried. '''
