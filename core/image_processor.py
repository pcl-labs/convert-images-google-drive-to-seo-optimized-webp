"""
Image resizing, compression, and conversion utilities.
"""

import os
import logging
from PIL import Image
import io
import json
import pillow_heif
from .constants import PORTRAIT_SIZE, LANDSCAPE_SIZE, DEFAULT_MAX_SIZE_KB

pillow_heif.register_heif_opener()

# Sizes and defaults are defined in core.constants

logger = logging.getLogger(__name__)


def _save_as_webp_under_size(img, max_size_kb, start_quality=80, min_quality=10, step=5):
    buffer = io.BytesIO()
    for q in range(start_quality, min_quality, -step):
        buffer.seek(0)
        buffer.truncate(0)
        img.save(buffer, format='WEBP', quality=q)
        size_kb = buffer.tell() / 1024
        if size_kb <= max_size_kb:
            return buffer.getvalue(), int(size_kb)
    # If not small enough, save at lowest quality
    buffer.seek(0)
    buffer.truncate(0)
    img.save(buffer, format='WEBP', quality=min_quality)
    return buffer.getvalue(), int(buffer.tell() / 1024)


def resize_image(input_path, output_path, target_size):
    """Resize image to target_size and save to output_path."""
    with Image.open(input_path) as img:
        img = img.convert('RGB')
        img = img.resize(target_size, Image.LANCZOS)
        img.save(output_path)


def compress_and_convert_to_webp(input_path, output_path, max_size_kb=DEFAULT_MAX_SIZE_KB, quality=80):
    """Compress and convert image to .webp under max_size_kb."""
    with Image.open(input_path) as img:
        img = img.convert('RGB')
        data, size_kb = _save_as_webp_under_size(img, max_size_kb, start_quality=quality, min_quality=10, step=5)
        with open(output_path, 'wb') as f:
            f.write(data)
        return size_kb <= max_size_kb


def extract_alt_text(filename):
    """Extract alt text from filename (without extension)."""
    name = os.path.splitext(os.path.basename(filename))[0]
    return name.replace('-', ' ').replace('_', ' ').replace('.', ' ').strip()


def update_alt_text_map(webp_filename, alt_text, map_path='alt_text_map.json'):
    """Update alt_text_map.json with new alt text."""
    if os.path.exists(map_path):
        try:
            with open(map_path, 'r') as f:
                alt_map = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read/parse {map_path}: {e}")
            alt_map = {}
    else:
        alt_map = {}
    alt_map[webp_filename] = alt_text
    tmp_path = f"{map_path}.tmp"
    try:
        with open(tmp_path, 'w') as f:
            json.dump(alt_map, f, indent=2)
        os.replace(tmp_path, map_path)
    except OSError as e:
        logger.error(f"Failed to write {map_path}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError as cleanup_err:
            logger.warning(f"Failed to remove temp file {tmp_path}: {cleanup_err}")
        raise


def process_image(input_path, output_dir, overwrite=False, skip_existing=False, versioned=False, max_size_kb=DEFAULT_MAX_SIZE_KB, alt_text_map_path='alt_text_map.json', seo_prefix=None):
    """Resize, compress, and convert image to .webp in output_dir. Update alt text map. Handle conflict logic. Optionally prefix output filename with seo_prefix."""
    base = os.path.basename(input_path)
    name, _ = os.path.splitext(base)
    if seo_prefix:
        out_name = f"{seo_prefix}-{name}.webp"
    else:
        out_name = f"{name}.webp"
    output_path = os.path.join(output_dir, out_name)
    if os.path.exists(output_path):
        if skip_existing:
            logger.info(f"Skipping existing: {output_path}")
            return output_path, 'skipped'
        if not overwrite and versioned:
            # Find next available versioned filename
            v = 2
            while True:
                if seo_prefix:
                    versioned_name = f"{seo_prefix}-{name}_v{v}.webp"
                else:
                    versioned_name = f"{name}_v{v}.webp"
                versioned_path = os.path.join(output_dir, versioned_name)
                if not os.path.exists(versioned_path):
                    output_path = versioned_path
                    break
                v += 1
        elif not overwrite:
            logger.info(f"Skipping (exists, no overwrite): {output_path}")
            return output_path, 'skipped'
    with Image.open(input_path) as img:
        # Convert to RGB before processing to handle RGBA, P, and other modes
        img = img.convert('RGB')
        w, h = img.size
        if h > w:
            target_size = PORTRAIT_SIZE
        else:
            target_size = LANDSCAPE_SIZE
        resized = img.resize(target_size, Image.LANCZOS)
        data, size_kb = _save_as_webp_under_size(resized, max_size_kb, start_quality=80, min_quality=10, step=5)
        with open(output_path, 'wb') as f:
            f.write(data)
        if size_kb <= max_size_kb:
            logger.info(f"Optimized: {output_path} ({int(size_kb)} KB)")
            alt_text = extract_alt_text(base)
            try:
                update_alt_text_map(os.path.basename(output_path), alt_text, alt_text_map_path)
            except Exception as e:
                logger.error(f"Failed to update alt text map for {output_path}: {e}")
            return output_path, 'ok'
        logger.info(f"Saved at lowest quality: {output_path}")
        alt_text = extract_alt_text(base)
        try:
            update_alt_text_map(os.path.basename(output_path), alt_text, alt_text_map_path)
        except Exception as e:
            logger.error(f"Failed to update alt text map for {output_path}: {e}")
    return output_path, 'low_quality'
 