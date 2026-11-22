"""Render SF Symbols to PNG masks so the web UI uses the same glyphs as the notch."""

from pathlib import Path
import re
import threading

CACHE = Path.home() / "Library/Caches/Luna/symbols"
_LOCK = threading.Lock()
_NAME = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)*$")


def render(name, size=64, weight="regular"):
    """PNG bytes of a white symbol on transparency, or None if the name isn't a system symbol."""
    if not _NAME.match(name) or len(name) > 80:
        return None
    path = CACHE / f"{name}@{size}-{weight}.png"
    if path.exists():
        return path.read_bytes()
    with _LOCK:
        data = _draw(name, size, weight)
    if data is None:
        return None
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        pass
    return data


def _draw(name, size, weight):
    import AppKit as AK

    weights = {"light": AK.NSFontWeightLight, "regular": AK.NSFontWeightRegular, "medium": AK.NSFontWeightMedium,
               "semibold": AK.NSFontWeightSemibold, "bold": AK.NSFontWeightBold}
    image = AK.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if image is None:
        return None
    config = AK.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
        size * 0.72, weights.get(weight, AK.NSFontWeightRegular), AK.NSImageSymbolScaleMedium)
    config = config.configurationByApplyingConfiguration_(
        AK.NSImageSymbolConfiguration.configurationWithHierarchicalColor_(AK.NSColor.whiteColor()))
    image = image.imageWithSymbolConfiguration_(config)
    rep = AK.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, AK.NSDeviceRGBColorSpace, 0, 0)
    context = AK.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AK.NSGraphicsContext.saveGraphicsState()
    try:
        AK.NSGraphicsContext.setCurrentContext_(context)
        w, h = image.size()
        scale = min(size / w, size / h) if w and h else 1
        w, h = w * scale, h * scale
        image.drawInRect_fromRect_operation_fraction_(((((size - w) / 2, (size - h) / 2), (w, h))), ((0, 0), (0, 0)),
                                                      AK.NSCompositingOperationSourceOver, 1.0)
        context.flushGraphics()
    finally:
        AK.NSGraphicsContext.restoreGraphicsState()
    return bytes(rep.representationUsingType_properties_(AK.NSBitmapImageFileTypePNG, {}))
