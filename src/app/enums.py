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
    OCR_DONE = "OCR_DONE"
    CLASSIFIED = "CLASSIFIED"
    EXTRACTED = "EXTRACTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    ASSOCIATED = "ASSOCIATED"
    ROUTED = "ROUTED"
    PUSHED = "PUSHED"
    FAILED = "FAILED"
    

class AuditAction(str, Enum):
    STATE_CHANGE = "state_change"
    VIEW = "view"
    EXPORT = "export"
    RECLASSIFY = "reclassify"
    RESOLVED = "resolved"
    DELETE = "delete"




