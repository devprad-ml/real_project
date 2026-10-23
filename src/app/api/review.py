''' GET /review: the demo review page. Static; it calls the API with the key the
reviewer pastes in. `ponytail:` a demo, not the M12 chatbot UI. '''

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["Review"])
PAGE = Path(__file__).resolve().parent.parent / "static" / "review.html"


@router.get("/review", include_in_schema=False)
def review_page() -> FileResponse:
    return FileResponse(PAGE)
