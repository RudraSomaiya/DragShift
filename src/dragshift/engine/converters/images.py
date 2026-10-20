"""Image converters (PRD §6.1 image row). DS-6 ships JPG; DS-13 adds the rest of the row.

Pillow is imported inside each function: the UI process imports this module to register Ops.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.kinds import Kind
from dragshift.engine.registry import op

if TYPE_CHECKING:
    from PIL.Image import Image

    from dragshift.engine.worker import JobContext

IMAGE = frozenset({Kind.IMAGE})


def _flatten_on_white(im: "Image") -> "Image":
    from PIL import Image

    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return im.convert("RGB")


@op(id="convert.image.jpg", wheel="convert", label="JPG", kinds=IMAGE, hide_if_same_ext="jpg")
def to_jpg(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    from PIL import Image

    outputs = []
    for src in paths:
        with Image.open(src) as im:
            rgb = _flatten_on_white(im)
        dst = ctx.output_path(src, ext="jpg")
        rgb.save(dst, format="JPEG", quality=int(params.get("quality", 85)))
        outputs.append(dst)
    return outputs
