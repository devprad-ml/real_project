''' GET /chat: the staff chatbot page. Static; it calls /ask and /documents/{id}/file
with the key the user pastes in. See m12.md. '''

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["Chat"])
PAGE = Path(__file__).resolve().parent.parent / "static" / "chat.html"


@router.get("/chat", include_in_schema=False)
def chat_page() -> FileResponse:
    return FileResponse(PAGE)
