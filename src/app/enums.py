from enum import Enum


class DocumentType(str, Enum):
    CLAIMS = "claims"
    APPEAL = "appeal"
    DENIAL = "denial"
    PRIOR_AUTH = "prior_auth"
    CORRESPONDENCE = "correspondence"
    CONSENT = "consent"
    MEDICAL_RECORD = "medical_record"
    UNKNOWN = "unknown"

class DocumentStatus(str, Enum):
    RECEIVED = "RECEIVED"
    NORMALIZED = "NORMALIZED"
    CLASSIFIED = "CLASSIFIED"
    EMBEDDED = "EMBEDDED"
    EXTRACTED = "EXTRACTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    ASSOCIATED = "ASSOCIATED"
    ROUTED = "ROUTED"
    PUSHED = "PUSHED"
    FAILED = "FAILED"
    

class JobKind(str, Enum):
    NORMALIZE = "normalize"
    CLASSIFY = "classify"
    EMBED = "embed"
    EXTRACT = "extract"


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class AuditAction(str, Enum):
    STATE_CHANGE = "state_change"
    VIEW = "view"
    EXPORT = "export"
    RECLASSIFY = "reclassify"
    RESOLVE = "resolve"
    DELETE = "delete"
    
    






class Role(str, Enum):
    REVIEWER = "reviewer"
    ADMIN = "admin"  # passes every role check
