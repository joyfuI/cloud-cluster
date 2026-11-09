"""Windows Shell virtual files backed by temporary, verified downloads."""

from ctypes import (
    POINTER,
    WINFUNCTYPE,
    Structure,
    WinDLL,
    WinError,
    byref,
    c_long,
    c_size_t,
    c_void_p,
    cast,
    memmove,
    string_at,
)
from ctypes.wintypes import BOOL, DWORD, HWND, LPCWSTR, UINT, ULONG, WORD
from pathlib import Path
from struct import Struct, pack
from tempfile import TemporaryDirectory
from uuid import UUID

from ..diagnostics import error_summary, logger
from ..domain import BackendError, ErrorKind

S_OK, E_NOINTERFACE, E_NOTIMPL, E_FAIL = 0, 0x80004002, 0x80004001, 0x80004005
COPYENGINE_E_USER_CANCELLED = 0x80270000
DV_E_FORMATETC, DV_E_LINDEX, DV_E_TYMED, DV_E_DVASPECT = (
    0x80040064,
    0x80040068,
    0x80040069,
    0x8004006B,
)
DROP, CANCEL, DEFAULT_CURSORS = 0x40100, 0x40101, 0x40102
COPY, MOVE = 1, 2
HGLOBAL, ISTREAM = 1, 4
CONTENT, LEFT_BUTTON = 1, 1
_FILE_DESCRIPTOR = Struct("<I32xI24xII520s")


class Format(Structure):
    _fields_ = [
        ("id", WORD),
        ("device", c_void_p),
        ("aspect", DWORD),
        ("index", c_long),
        ("medium", DWORD),
    ]


class Medium(Structure):
    _fields_ = [("kind", DWORD), ("value", c_void_p), ("owner", c_void_p)]


def _bind(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, args
    return function


_ole, _shell, _stream, _kernel, _user = map(
    WinDLL, ("ole32", "shell32", "shlwapi", "kernel32", "user32")
)
_do_drag_drop = _bind(_ole, "DoDragDrop", c_long, c_void_p, c_void_p, DWORD, POINTER(DWORD))
_release_medium = _bind(_ole, "ReleaseStgMedium", None, POINTER(Medium))
_enum_formats = _bind(
    _shell, "SHCreateStdEnumFmtEtc", c_long, UINT, POINTER(Format), POINTER(c_void_p)
)
_open_stream = _bind(
    _stream,
    "SHCreateStreamOnFileEx",
    c_long,
    LPCWSTR,
    DWORD,
    DWORD,
    BOOL,
    c_void_p,
    POINTER(c_void_p),
)
_alloc = _bind(_kernel, "GlobalAlloc", c_void_p, UINT, c_size_t)
_lock = _bind(_kernel, "GlobalLock", c_void_p, c_void_p)
_unlock = _bind(_kernel, "GlobalUnlock", BOOL, c_void_p)
_free = _bind(_kernel, "GlobalFree", c_void_p, c_void_p)
_current_process = _bind(_kernel, "GetCurrentProcessId", DWORD)
_window_process = _bind(_user, "GetWindowThreadProcessId", DWORD, HWND, POINTER(DWORD))
_register = _bind(_user, "RegisterClipboardFormatW", WORD, LPCWSTR)
_load_cursor = _bind(_user, "LoadCursorW", c_void_p, c_void_p, c_void_p)
_set_cursor = _bind(_user, "SetCursor", c_void_p, c_void_p)

# A Shell target may hold the COM object past the end of DoDragDrop.
_live = {}


class FileDrag:
    def __init__(self, internal_mime, payload, files, stage, finished):
        self._stage, self._finished = stage, finished
        self._paths = None
        self._wait_for_file = None
        self._temp = None
        self._released = self._local = self._running = self._async = False
        self._async_enabled = True
        self._effect = 0
        self._refs = 1
        self._interfaces, self._tables = {}, []
        self._file_count = len(files)
        self._contents = _register("FileContents")
        self._in_drag = _register("InShellDragLoop")
        self._globals = {
            _register(internal_mime): payload,
            _register("FileGroupDescriptorW"): pack("<I", len(files))
            + b"".join(
                _FILE_DESCRIPTOR.pack(
                    0x80000004 | (0x40 if size is not None else 0),  # UNICODE, ATTRIBUTES, FILESIZE
                    0x80,  # FILE_ATTRIBUTE_NORMAL
                    (size or 0) >> 32,
                    (size or 0) & 0xFFFFFFFF,
                    name.encode("utf-16-le"),
                )
                for name, size in files
            ),
            _register("Preferred DropEffect"): pack("<I", COPY),
            self._in_drag: b"",
        }
        self._effect_formats = {
            _register(name)
            for name in ("Performed DropEffect", "Logical Performed DropEffect", "Paste Succeeded")
        }
        if not all((*self._globals, self._contents, *self._effect_formats)):
            raise OSError("Windows 드래그 형식을 등록할 수 없습니다.")
        self._formats = (Format * (len(self._globals) + 1))(
            *(Format(kind, None, CONTENT, -1, HGLOBAL) for kind in self._globals),
            Format(self._contents, None, CONTENT, 0, ISTREAM),
        )

        self.data = self._interface(
            "0000010e-0000-0000-c000-000000000046",  # IDataObject
            (self._get_data, POINTER(Format), POINTER(Medium)),
            (self._unsupported, POINTER(Format), POINTER(Medium)),  # GetDataHere
            (self._query_data, POINTER(Format)),
            (self._canonical_format, POINTER(Format), POINTER(Format)),
            (self._set_data, POINTER(Format), POINTER(Medium), BOOL),
            (self._enumerate, DWORD, POINTER(c_void_p)),
            (self._unsupported, POINTER(Format), DWORD, c_void_p, POINTER(DWORD)),  # DAdvise
            (self._unsupported, DWORD),  # DUnadvise
            (self._unsupported, POINTER(c_void_p)),  # EnumDAdvise
        )
        self._interfaces[UUID("00000000-0000-0000-c000-000000000046").bytes_le] = self.data
        self.source = self._interface(
            "00000121-0000-0000-c000-000000000046",  # IDropSource
            (self._continue_drag, BOOL, DWORD),
            (self._feedback, DWORD),
        )
        self._interface(
            "0000012b-0000-0000-c000-000000000046",  # IDropSourceNotify
            (self._enter_target, HWND),
            (self._leave_target,),
        )
        self._interface(
            "3d8b0590-f691-11d2-8ea9-006097df5bd4",  # IDataObjectAsyncCapability
            (self._set_async, BOOL),
            (self._get_async, POINTER(BOOL)),
            (self._start_operation, c_void_p),
            (self._in_operation, POINTER(BOOL)),
            (self._end_operation, c_long, c_void_p, DWORD),
        )
        _live[self.data] = self

    def _interface(self, iid, *methods):
        callbacks = (
            WINFUNCTYPE(c_long, c_void_p, c_void_p, POINTER(c_void_p))(self._query_interface),
            WINFUNCTYPE(ULONG, c_void_p)(self._add_ref),
            WINFUNCTYPE(ULONG, c_void_p)(self._release),
            *(WINFUNCTYPE(c_long, c_void_p, *args)(method) for method, *args in methods),
        )
        vtable = (c_void_p * len(callbacks))(*(cast(cb, c_void_p) for cb in callbacks))
        interface = cast(vtable, c_void_p)
        address = cast(byref(interface), c_void_p).value
        self._tables.append((interface, vtable, callbacks))
        self._interfaces[UUID(iid).bytes_le] = address
        return address

    def _query_interface(self, this, iid, out):
        address = self._interfaces.get(string_at(iid, 16))
        out[0] = address
        if address is None:
            return E_NOINTERFACE
        self._add_ref(this)
        return S_OK

    def _add_ref(self, this):
        self._refs += 1
        return self._refs

    def _release(self, this):
        self._refs -= 1
        if self._refs == 0:
            _live.pop(self.data)
            # A disconnected target can release the object without calling EndOperation.
            self._async = False
            self._finish()
            if self._temp is not None:
                try:
                    self._temp.cleanup()
                except OSError as error:
                    logger.warning("drag temporary cleanup failed error=%s", error_summary(error))
        return self._refs

    def _query_data(self, this, fmt):
        request = fmt.contents
        if request.aspect != CONTENT:
            return DV_E_DVASPECT
        if request.id == self._contents:
            if not 0 <= request.index < self._file_count:
                return DV_E_LINDEX
            return S_OK if request.medium & ISTREAM else DV_E_TYMED
        if request.id in self._globals:
            return S_OK if request.medium & HGLOBAL else DV_E_TYMED
        return DV_E_FORMATETC

    def _get_data(self, this, fmt, medium):
        medium[0] = Medium()
        result = self._query_data(this, fmt)
        if result != S_OK:
            return result
        request = fmt.contents
        if request.id == self._contents:
            return self._get_file(request.index, medium)
        data = self._globals[request.id]
        if request.id == self._in_drag:
            data = pack("<I", int(not self._released))
        handle = _alloc(2, len(data))  # GMEM_MOVEABLE for OLE ownership transfer
        if not handle:
            return E_FAIL
        address = _lock(handle)
        if not address:
            _free(handle)
            return E_FAIL
        memmove(address, data, len(data))
        _unlock(handle)
        medium[0] = Medium(HGLOBAL, handle, None)
        return S_OK

    def _get_file(self, index, medium):
        if not self._released or self._local:
            return E_FAIL
        try:
            if self._paths is None:
                if self._finished is None:
                    return E_FAIL
                self._paths = ()
                self._temp = TemporaryDirectory(prefix="cloud-cluster-drag-")
                directory = Path(self._temp.name)
                self._paths = tuple(directory / str(i) for i in range(self._file_count))
                self._wait_for_file = self._stage(self._paths, self._hold_staging)
            if self._wait_for_file is None or not self._wait_for_file(index):
                return E_FAIL
        except Exception as error:
            if isinstance(error, BackendError) and error.kind == ErrorKind.CANCELLED:
                return COPYENGINE_E_USER_CANCELLED
            logger.error("drag download failed error=%s", error_summary(error))
            return E_FAIL
        stream = c_void_p()
        result = _open_stream(
            str(self._paths[index]), 0x20, 0, False, None, byref(stream)
        )  # STGM_SHARE_DENY_WRITE
        if result == S_OK:
            # ReleaseStgMedium closes the stream before releasing its owner.
            self._add_ref(self.data)
            medium[0] = Medium(ISTREAM, stream, self.data)
        return result

    def _hold_staging(self):
        # A target can release the data object before every queued write stops.
        self._add_ref(self.data)
        return lambda: self._release(self.data)

    @staticmethod
    def _unsupported(*args):
        return E_NOTIMPL

    @staticmethod
    def _canonical_format(this, fmt, out):
        out.contents.device = None
        return 0x00040130  # DATA_S_SAMEFORMATETC

    def _set_data(self, this, fmt, medium, release):
        if fmt.contents.id not in self._effect_formats:
            return E_NOTIMPL
        if release:
            _release_medium(medium)
        return S_OK

    def _enumerate(self, this, direction, out):
        if direction != 1:  # DATADIR_GET
            out[0] = None
            return E_NOTIMPL
        return _enum_formats(len(self._formats), self._formats, out)

    def _enter_target(self, this, window):
        process = DWORD()
        _window_process(window, byref(process))
        self._local = process.value == _current_process()
        return S_OK

    def _leave_target(self, this):
        self._local = False
        self._effect = 0
        return S_OK

    def _continue_drag(self, this, escape, keys):
        if escape:
            return CANCEL
        if keys & LEFT_BUTTON:
            return S_OK
        if not self._local and not self._effect & COPY:
            return CANCEL
        self._released = True
        return DROP

    def _feedback(self, this, effect):
        self._effect = effect
        if not self._local and effect & MOVE and not effect & COPY:
            _set_cursor(_load_cursor(None, 32648))  # IDC_NO
            return S_OK
        return DEFAULT_CURSORS

    def _set_async(self, this, enabled):
        self._async_enabled = bool(enabled)
        return S_OK

    def _get_async(self, this, enabled):
        enabled[0] = self._async_enabled
        return S_OK

    def _start_operation(self, this, reserved):
        self._async = True
        return S_OK

    def _in_operation(self, this, active):
        active[0] = self._async
        return S_OK

    def _end_operation(self, this, result, reserved, effect):
        self._async = False
        self._finish()
        return S_OK

    def _finish(self):
        if not self._running and not self._async and self._finished is not None:
            finished, self._finished = self._finished, None
            finished()

    def run(self):
        self._running = True
        try:
            effect = DWORD()
            result = _do_drag_drop(self.data, self.source, COPY | MOVE, byref(effect))
            if result < 0 and (result & 0xFFFFFFFF) != COPYENGINE_E_USER_CANCELLED:
                raise WinError(result)
        finally:
            self._running = False
            try:
                self._finish()
            finally:
                self._release(self.data)
