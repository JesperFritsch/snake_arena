from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel

from snake_sim.map_utils.general import get_map_files_mapping, get_maps_info

router = APIRouter(prefix="/maps", tags=["maps"])

_MAX_DIM = 20


class MapInfo(BaseModel):
    name: str
    height: int
    width: int


@router.get("", response_model=list[MapInfo])
def list_maps() -> list[MapInfo]:
    """Return all maps whose dimensions fit within the test-match grid limit."""
    info = get_maps_info()
    return [
        MapInfo(name=name, height=dims["height"], width=dims["width"])
        for name, dims in sorted(info.items())
        if dims["height"] <= _MAX_DIM and dims["width"] <= _MAX_DIM
    ]


@router.get("/{name}/image")
def get_map_image(name: str) -> Response:
    """Return the raw PNG for the named map."""
    mapping = get_map_files_mapping()
    if name not in mapping:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "map not found")
    data = mapping[name].read_bytes()
    return Response(content=data, media_type="image/png")
