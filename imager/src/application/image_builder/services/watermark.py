"""Generic text branding with bounded ROI memory and a legible tiny-image footer."""
from imager_config.loader import config
from imager_contracts import validate_watermark_text
from imager_contracts.protocol import COLOR_CHANNELS
from src.shared_kernel.resource_limits import configure_decoder_limits, require_memory

configure_decoder_limits()

import cv2
import numpy as np


def _measure(text, height, style):
    unit_height = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1, 1)[0][1]
    scale = height / unit_height
    stroke = max(1, round(height * style.stroke_fraction))
    outline = stroke + 2 * style.outline_pixels
    (width, height), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, outline)
    padding = max(style.padding_pixels, outline)
    return scale, stroke, outline, width, height, baseline, padding


def _geometry(image, text, style):
    image_height, image_width = image.shape[:2]
    side = min(image_height, image_width)
    margin = max(style.min_margin_pixels, round(side * style.margin_fraction))
    target_height = min(style.max_font_height_pixels,
                        max(style.min_font_height_pixels, side * style.font_height_fraction))
    metrics = _measure(text, target_height, style)
    preferred_width = image_width * style.preferred_width_fraction
    if metrics[3] > preferred_width:
        target_height = max(style.min_font_height_pixels, target_height * preferred_width / metrics[3])
        metrics = _measure(text, target_height, style)
    _, _, _, width, height, baseline, padding = metrics
    needed_width = width + 2 * (padding + margin)
    needed_height = height + baseline + 2 * (padding + margin)
    if needed_width > image_width or needed_height > image_height:
        # The original mosaic is retained unchanged above a complete, legible
        # label; scaling or truncating the link would destroy its usefulness.
        metrics = _measure(text, style.min_font_height_pixels, style)
        _, _, _, width, height, baseline, padding = metrics
        needed_width = width + 2 * (padding + margin)
        needed_height = height + baseline + 2 * (padding + margin)
        return metrics, margin, max(image_width, needed_width), image_height + needed_height
    return metrics, margin, image_width, image_height


def apply_watermark(image, text):
    """Return the branded BGR image; ordinary-size images are changed in place.

    Text is a generic frozen request value. Telegram identities, APIs and file
    transfers remain outside the rendering core. None preserves legacy pixels.
    """
    validate_watermark_text(text)
    if text is None:
        return image
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
            or image.ndim != 3 or image.shape[2] != COLOR_CHANNELS
            or not image.size or not image.flags.c_contiguous):
        raise ValueError('Watermark requires a nonempty contiguous uint8 BGR image')
    metrics, margin, width, height = _geometry(image, text, config.watermark)
    if width * height > config.limits.output_pixels:
        raise ValueError('Watermarked output exceeds the core pixel budget')
    if (height, width) != image.shape[:2]:
        require_memory(width * height * COLOR_CHANNELS)
        image = cv2.copyMakeBorder(
            image, 0, height - image.shape[0], 0, width - image.shape[1],
            cv2.BORDER_CONSTANT, value=config.watermark.background_bgr,
        )
    scale, stroke, outline, text_width, text_height, baseline, padding = metrics
    box_width, box_height = text_width + 2 * padding, text_height + baseline + 2 * padding
    x, y = width - margin - box_width, height - margin - box_height
    roi = image[y:y + box_height, x:x + box_width]
    if config.watermark.background_opacity:
        require_memory(roi.nbytes * 2)
        background = np.empty_like(roi)
        background[:] = config.watermark.background_bgr
        cv2.addWeighted(background, config.watermark.background_opacity,
                        roi, 1 - config.watermark.background_opacity, 0, dst=roi)
    origin = padding, padding + text_height
    cv2.putText(roi, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale,
                config.watermark.background_bgr, outline, cv2.LINE_AA)
    cv2.putText(roi, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale,
                config.watermark.foreground_bgr, stroke, cv2.LINE_AA)
    return image
