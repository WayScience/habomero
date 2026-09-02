"""Thermo CX7 high-content-screening filename parsing and Plate/Well import support.

Filenames encode plate structure: ``<computer>_<plateID>_<well>f<field>d<channel>``,
e.g. ``CARD-CelIns-CX7_260803130001_B02f00d0`` (computer ``CARD-CelIns-CX7``, plate
``260803130001``, well ``B02``, field ``f00``, channel ``d0``). Fields are numbered
1-25 in a center-out spiral across a 5x5 grid per well; ``FIELD_GRID`` maps each
zero-based field index to its ``(row, col)`` position in that grid.
"""

from __future__ import annotations

import re
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

FILENAME_RE = re.compile(
    r"^(?P<computer>.+)_(?P<plate_id>\d+)_(?P<well>[A-H]\d{2})"
    r"f(?P<field>\d{2})d(?P<channel>\d+)$"
)

# Zero-based field index -> (row, col) in the 5x5 per-well grid, center-out spiral.
# Field 1 (index 0, filename token f00) sits at the center (2, 2).
FIELD_GRID: dict[int, tuple[int, int]] = {
    0: (2, 2),
    1: (2, 3),
    2: (3, 3),
    3: (3, 2),
    4: (3, 1),
    5: (2, 1),
    6: (1, 1),
    7: (1, 2),
    8: (1, 3),
    9: (1, 4),
    10: (2, 4),
    11: (3, 4),
    12: (4, 4),
    13: (4, 3),
    14: (4, 2),
    15: (4, 1),
    16: (4, 0),
    17: (3, 0),
    18: (2, 0),
    19: (1, 0),
    20: (0, 0),
    21: (0, 1),
    22: (0, 2),
    23: (0, 3),
    24: (0, 4),
}

OME_NAMESPACE = "http://www.openmicroscopy.org/Schemas/OME/2016-06"


class HcsError(ValueError):
    """Raised for invalid HCS filenames or pixel metadata."""


@dataclass(frozen=True)
class HcsFileInfo:
    """Parsed identity of a single Thermo CX7 HCS channel file."""

    computer: str
    plate_id: str
    well: str  # e.g. "B02"
    field: str  # zero-padded filename token, e.g. "f00"
    channel: int  # e.g. 0


def parse_filename(stem: str) -> HcsFileInfo | None:
    """Parse a Thermo CX7 filename stem, or return None if it doesn't match."""

    match = FILENAME_RE.match(stem)
    if not match:
        return None
    return HcsFileInfo(
        computer=match.group("computer"),
        plate_id=match.group("plate_id"),
        well=match.group("well"),
        field=f"f{match.group('field')}",
        channel=int(match.group("channel")),
    )


def well_row_index(well: str) -> int:
    """Return the zero-based plate row index for a well like 'B02' (B -> 1)."""

    return ord(well[0].upper()) - ord("A")


def well_column_index(well: str) -> int:
    """Return the zero-based plate column index for a well like 'B02' (02 -> 1)."""

    return int(well[1:]) - 1


def field_index(field_token: str) -> int:
    """Return the zero-based field index for a token like 'f00' (-> 0)."""

    return int(field_token.removeprefix("f"))


def field_grid_position(field_token: str) -> tuple[int, int]:
    """Return the (row, col) position of a field within its 5x5 acquisition grid."""

    index = field_index(field_token)
    if index not in FIELD_GRID:
        raise HcsError(f"Unknown field index: {field_token}")
    return FIELD_GRID[index]


_TIFF_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}


def read_tiff_geometry(path: Path) -> tuple[int, int, int, int]:
    """Read (width, height, bits_per_sample, sample_format) from a baseline TIFF."""

    return parse_tiff_geometry(path.read_bytes(), label=str(path))


def parse_tiff_geometry(
    data: bytes, label: str = "<bytes>"
) -> tuple[int, int, int, int]:
    """Parse (width, height, bits_per_sample, sample_format) from a baseline TIFF."""

    byteorder = data[:2]
    if byteorder not in (b"II", b"MM"):
        raise HcsError(f"Not a TIFF file: {label}")
    fmt = "<" if byteorder == b"II" else ">"
    _magic, ifd_offset = struct.unpack_from(fmt + "HI", data, 2)
    (n_entries,) = struct.unpack_from(fmt + "H", data, ifd_offset)
    tags: dict[int, tuple[int, int, int]] = {}
    for i in range(n_entries):
        entry_off = ifd_offset + 2 + i * 12
        tag, typ, count, value_offset = struct.unpack_from(
            fmt + "HHII", data, entry_off
        )
        tags[tag] = (typ, count, value_offset)

    def tag_val(tagnum: int, default: int) -> int:
        if tagnum not in tags:
            return default
        typ, count, value_offset = tags[tagnum]
        size = _TIFF_TYPE_SIZES.get(typ, 4) * count
        raw = (
            struct.pack(fmt + "I", value_offset)[:size]
            if size <= 4  # noqa: PLR2004
            else data[value_offset : value_offset + size]
        )
        if typ == 3:  # noqa: PLR2004
            return struct.unpack(fmt + "H", raw[:2])[0]
        if typ == 4:  # noqa: PLR2004
            return struct.unpack(fmt + "I", raw[:4])[0]
        return default

    width = tag_val(256, 0)
    height = tag_val(257, 0)
    bits_per_sample = tag_val(258, 1)
    sample_format = tag_val(339, 1)  # 1=unsigned int (TIFF default when tag absent)
    if not width or not height:
        raise HcsError(f"Could not read TIFF dimensions: {label}")
    return width, height, bits_per_sample, sample_format


def ome_pixel_type(bits_per_sample: int, sample_format: int) -> str:
    """Map TIFF BitsPerSample/SampleFormat to an OME-XML Pixels Type value."""

    if sample_format == 3:  # noqa: PLR2004
        return "float" if bits_per_sample <= 32 else "double"  # noqa: PLR2004
    signed = sample_format == 2  # noqa: PLR2004
    prefix = "int" if signed else "uint"
    if bits_per_sample in (8, 16, 32):
        return f"{prefix}{bits_per_sample}"
    raise HcsError(f"Unsupported bits-per-sample: {bits_per_sample}")


def generate_companion_xml(
    image_name: str,
    size_x: int,
    size_y: int,
    pixel_type: str,
    channel_relative_paths: list[str],
) -> str:
    """Build a Bio-Formats `.companion.ome` XML for one multi-channel field Image.

    `channel_relative_paths` must be relative to the companion file's own eventual
    directory, and must not contain `..` parent-traversal segments (Bio-Formats'
    checksum verification fails silently for symlink-transferred multi-file imports
    that reference files via parent-directory-traversing relative paths).
    """

    for rel_path in channel_relative_paths:
        if ".." in Path(rel_path).parts:
            raise HcsError(
                f"Companion file references must not contain '..': {rel_path}"
            )

    size_c = len(channel_relative_paths)
    channels = "\n".join(
        f'      <Channel ID="Channel:0:{c}" SamplesPerPixel="1"/>'
        for c in range(size_c)
    )
    tiffdata = "\n".join(
        f'      <TiffData IFD="0" FirstC="{c}" FirstZ="0" FirstT="0" PlaneCount="1">\n'
        f'        <UUID FileName="{rel_path}">urn:uuid:{uuid.uuid4()}</UUID>\n'
        f"      </TiffData>"
        for c, rel_path in enumerate(channel_relative_paths)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OME xmlns="{OME_NAMESPACE}"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xsi:schemaLocation="{OME_NAMESPACE} {OME_NAMESPACE}/ome.xsd">
  <Image ID="Image:0" Name="{image_name}">
    <Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="{pixel_type}"
            SizeX="{size_x}" SizeY="{size_y}" SizeC="{size_c}" SizeZ="1" SizeT="1">
{channels}
{tiffdata}
    </Pixels>
  </Image>
</OME>
"""


def ensure_shadow_symlink(
    shadow_root: Path, root_key: str, plate_key: str, real_plate_dir: str
) -> Path:
    """Create (idempotently) a persistent symlink to a plate's real source directory.

    Returns the plate's shadow directory (containing the `source` symlink), which is
    where companion files for this plate should be written, referencing files as
    `source/<filename>` — a downward-only relative path that avoids the `..`
    checksum-verification bug in Bio-Formats' companion-file import.
    """

    plate_dir = shadow_root / root_key / plate_key
    plate_dir.mkdir(parents=True, exist_ok=True)
    source_link = plate_dir / "source"
    if source_link.is_symlink():
        if source_link.resolve() != Path(real_plate_dir).resolve():
            raise HcsError(
                f"Shadow symlink {source_link} already points elsewhere "
                f"({source_link.resolve()} != {real_plate_dir})"
            )
    elif source_link.exists():
        raise HcsError(f"Shadow path exists and is not a symlink: {source_link}")
    else:
        source_link.symlink_to(real_plate_dir)
    return plate_dir
