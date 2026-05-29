"""Build a multi-resolution Windows ``app.ico`` from a source PNG logo."""

import os

from PIL import Image

SOURCE_FILE = "logo.png"
ICON_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def convert(source_file: str = SOURCE_FILE, output_file: str = "app.ico") -> bool:
    """Convert ``source_file`` into a Windows ``.ico`` with standard sizes.

    Args:
        source_file: Path to the source raster image (e.g. a PNG logo).
        output_file: Destination ``.ico`` path.

    Returns:
        ``True`` if the icon was written, ``False`` if the source is missing.
    """
    if not os.path.exists(source_file):
        print(f"Error: Could not find '{source_file}' in this folder.")
        return False

    with Image.open(source_file) as img:
        img.save(output_file, sizes=ICON_SIZES)
    print(f"Success! '{output_file}' has been created.")
    return True


if __name__ == "__main__":
    convert()