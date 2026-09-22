#!/usr/bin/env python3
"""Inspect original brand artwork; emit framing values without editing pixels."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

from PIL import Image


def inspect_asset(path: Path) -> dict:
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as source:
        if getattr(source, 'n_frames', 1) != 1:
            raise ValueError('Use a single-frame brand asset')
        rgba = source.convert('RGBA')
    alpha = rgba.getchannel('A')
    bounds = alpha.getbbox()
    if bounds is None:
        raise ValueError('The image is fully transparent')
    left, top, right, bottom = bounds
    width, height = rgba.size
    artwork_width, artwork_height = right - left, bottom - top
    return {
        'image': str(path.resolve()),
        'sha256': hashlib.sha256(raw).hexdigest(),
        'pixelSize': [width, height],
        'alphaBoundsPixels': list(bounds),
        'artworkSizePixels': [artwork_width, artwork_height],
        'artworkAspectRatio': artwork_width / artwork_height,
        'fullyOpaque': alpha.getextrema() == (255, 255),
        'uvBoundsTopLeftOrigin': [left / width, top / height, right / width, bottom / height],
        'uvBoundsBottomLeftOrigin': [left / width, 1 - bottom / height, right / width, 1 - top / height],
        'cssFrame': {
            'wrapperAspectRatio': artwork_width / artwork_height,
            'imageWidthPercent': width / artwork_width * 100,
            'imageLeftPercent': -left / artwork_width * 100,
            'imageTopPercent': -top / artwork_height * 100,
            'imageHeight': 'auto',
            'wrapperOverflow': 'hidden',
        },
        'scope': 'Original bytes and alpha occupancy only; placement and optical centering require visual review.',
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='Optional JSON destination outside the source image')
    args = parser.parse_args()
    try:
        if args.output and args.output.resolve() == args.image.resolve():
            raise ValueError('Output must not overwrite the source image')
        report = inspect_asset(args.image)
        text = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=args.output.parent, prefix=args.output.name + '.', suffix='.tmp', delete=False) as stream:
                    temp_path = Path(stream.name)
                    stream.write(text)
                os.replace(temp_path, args.output)
            finally:
                if temp_path and temp_path.exists():
                    temp_path.unlink()
        print(text, end='')
        return 0
    except (OSError, ValueError) as error:
        parser.exit(2, f'Brand inspection failed: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
