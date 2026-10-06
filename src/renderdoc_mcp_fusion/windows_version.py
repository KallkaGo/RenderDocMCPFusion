"""Update only display strings in a copied Windows executable's version tree."""

from dataclasses import dataclass
from pathlib import Path
import struct


_LABELS = {
    "FileDescription": "RDC MCP",
    "ProductName": "RDC MCP",
    "InternalName": "RDCMCP",
    "OriginalFilename": "RDCMCP.exe",
}


@dataclass
class _Node:
    key: str
    kind: int
    value: bytes
    children: list


def _align(offset):
    return (offset + 3) & ~3


def _parse(data, start=0, limit=None):
    limit = len(data) if limit is None else limit
    if start + 6 > limit:
        raise ValueError("Truncated version resource header")
    length, value_length, kind = struct.unpack_from("<HHH", data, start)
    end = start + length
    if length < 6 or end > limit or kind not in (0, 1):
        raise ValueError("Invalid version resource node")
    cursor = start + 6
    key_end = cursor
    while key_end + 2 <= end and data[key_end:key_end + 2] != b"\0\0":
        key_end += 2
    if key_end + 2 > end:
        raise ValueError("Unterminated version resource key")
    key = data[cursor:key_end].decode("utf-16-le")
    cursor = _align(key_end + 2)
    byte_length = value_length * 2 if kind == 1 else value_length
    if cursor + byte_length > end:
        raise ValueError("Truncated version resource value")
    value = data[cursor:cursor + byte_length]
    cursor = _align(cursor + byte_length)
    children = []
    while cursor < end:
        if not any(data[cursor:end]):
            break  # Optional trailing alignment bytes.
        child, child_end = _parse(data, cursor, end)
        children.append(child)
        cursor = _align(child_end)
    return _Node(key, kind, value, children), end


def _encode(node):
    value_length = len(node.value) // 2 if node.kind == 1 else len(node.value)
    data = bytearray(struct.pack("<HHH", 0, value_length, node.kind))
    data.extend((node.key + "\0").encode("utf-16-le"))
    data.extend(b"\0" * (_align(len(data)) - len(data)))
    data.extend(node.value)
    for child in node.children:
        data.extend(b"\0" * (_align(len(data)) - len(data)))
        data.extend(_encode(child))
    if len(data) > 65535:
        raise ValueError("Version resource node exceeds WORD length")
    struct.pack_into("<H", data, 0, len(data))
    return bytes(data)


def _replace_labels(root):
    if root.key != "VS_VERSION_INFO":
        raise ValueError("Unrecognized version resource root")
    tables = [table for info in root.children if info.key == "StringFileInfo"
              for table in info.children]
    if not tables:
        raise ValueError("Executable has no version string tables")
    for table in tables:
        existing = {child.key: child for child in table.children}
        for key, label in _LABELS.items():
            value = (label + "\0").encode("utf-16-le")
            if key in existing:
                existing[key].kind = 1
                existing[key].value = value
            else:
                table.children.append(_Node(key, 1, value, []))


def set_hub_description(executable):
    """Label all version-resource languages; preserve versions and other fields.

    Call only on a private runtime copy before launching it. Resource updates
    change the executable's bytes and can invalidate an embedded signature.
    """
    import win32api

    executable = str(Path(executable).resolve())
    module = win32api.LoadLibraryEx(executable, 0, 2)  # LOAD_LIBRARY_AS_DATAFILE
    replacements = []
    try:
        for name in win32api.EnumResourceNames(module, 16):  # RT_VERSION
            for language in win32api.EnumResourceLanguages(module, 16, name):
                data = win32api.LoadResource(module, 16, name, language)
                root, _ = _parse(data)
                _replace_labels(root)
                replacements.append((name, language, _encode(root)))
    finally:
        win32api.FreeLibrary(module)
    if not replacements:
        raise ValueError("Executable has no version resources")
    update = win32api.BeginUpdateResource(executable, False)
    try:
        for name, language, data in replacements:
            win32api.UpdateResource(update, 16, name, data, language)
    except BaseException:
        win32api.EndUpdateResource(update, True)
        raise
    win32api.EndUpdateResource(update, False)
