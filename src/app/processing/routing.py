''' Routing rules: which departmental queue a document goes to (BRD FR-008).

ponytail: keyed on doc_type only, shared by every tenant. BRD FR-008 also names line
of business; add it to the key (from the matched member) when the first LOB-specific
rule exists, and move RULES to a table when a tenant needs its own.
'''

from app.enums import DocumentType, RouteQueue
from app.errors import PermanentError
from app.processing.extract import FIELD_SCHEMAS

RULES: dict[DocumentType, RouteQueue] = {
    DocumentType.CLAIMS: RouteQueue.CLAIMS_OPS,
    DocumentType.DENIAL: RouteQueue.CLAIMS_OPS,
    # BRD §6: Claims Operations needs "attached medical records".
    DocumentType.MEDICAL_RECORD: RouteQueue.CLAIMS_OPS,
    DocumentType.APPEAL: RouteQueue.APPEALS,
    DocumentType.PRIOR_AUTH: RouteQueue.UTILIZATION_MGMT,
    DocumentType.CONSENT: RouteQueue.MEMBER_SERVICES,
    DocumentType.CORRESPONDENCE: RouteQueue.MEMBER_SERVICES,
}


def route(doc_type: DocumentType, member_bound: bool) -> RouteQueue:
    ''' A document that should name a member but is bound to none (a reviewer said
    the member is not in the MPI) goes to member services for eligibility research:
    claims ops cannot adjudicate for nobody. A doc type with no rule is a rules bug,
    and must fail loudly rather than land in some default queue. '''
    if doc_type not in RULES:
        raise PermanentError(f"no routing rule for {doc_type.value}")
    if not member_bound and "member_id" in FIELD_SCHEMAS[doc_type]:
        return RouteQueue.MEMBER_SERVICES
    return RULES[doc_type]
