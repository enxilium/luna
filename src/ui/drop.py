"""Read local file references from macOS drag pasteboards without opening them."""

import AppKit as AK
from Foundation import NSURL


def file_paths(pasteboard):
    urls = pasteboard.readObjectsForClasses_options_([NSURL], {
        AK.NSPasteboardURLReadingFileURLsOnlyKey: True,
    }) or []
    return [str(url.path()) for url in urls
            if url.isFileURL() and url.host() in (None, "", "localhost") and url.path() and "\0" not in url.path()]


def has_files(pasteboard):
    return pasteboard.availableTypeFromArray_([AK.NSPasteboardTypeFileURL]) is not None
