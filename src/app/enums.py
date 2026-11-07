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

class AuditAction(str, Enum):
    # Some members are no longer written, but old append-only rows still hold them.
    STATE_CHANGE = "state_change"
    VIEW = "view"
    EXPORT = "export"
    RECLASSIFY = "reclassify"
    RESOLVE = "resolve"
    DELETE = "delete"
    REQUEUE = "requeue"
    VERDICT = "verdict"  # the downstream's answer, when it changes no status
    
    


class Role(str, Enum):
    REVIEWER = "reviewer"
    ADMIN = "admin"  # passes every role check
