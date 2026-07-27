"""RFC 6455 WebSocket codec — pure functions over bytes (R-29 / T3).

Kept a separate module on purpose (GD-15): it is the most unit-testable part of
the server, and every framing rule here is checkable against the RFC's own
vectors with no socket, no event loop and no clock.

**No I/O.** Nothing in this file opens, reads or writes a socket; the transport
lives in `server.py`. That split is what lets `tests/test_ws.py` assert
protocol conformance byte-for-byte instead of racing a live connection.

What it does beyond the prior art: `monitor_server.py:324-354` parses just
enough of a client frame to find CLOSE and **deletes payloads unread** (it is a
one-way stream, so it never needs them). Touch's socket eventually carries
control intents, so the codec here **unmasks** client frames, reassembles
fragmented messages, validates UTF-8 on text messages, enforces size caps and
maps every violation to the close code RFC 6455 §7.4.1 assigns. The prior art's
skip-and-discard path is kept as :func:`drain_frames` — byte-compatible with
`parse_client_frames`, for a read-only socket that must not allocate payloads
it will never look at.

Frame rules enforced (each maps to one test):

* server→client frames are never masked, client→server frames always are
  (§5.1); a violation in either direction is 1002.
* RSV1-3 set with no negotiated extension is 1002; unknown opcodes are 1002.
* control frames may not be fragmented and may not exceed 125 payload bytes
  (§5.5); the length must use its *minimal* encoding (§5.2).
* a continuation with no start frame, or a new data frame mid-message, is 1002.
* a message over ``max_message_bytes`` is 1009; invalid UTF-8 in a text
  message, or in a close reason, is 1007; a 1-byte close payload is 1002 and a
  reserved/invalid close code is 1002 (§7.4.1).
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

__all__ = [
    "WS_GUID",
    "OP_CONT", "OP_TEXT", "OP_BINARY", "OP_CLOSE", "OP_PING", "OP_PONG",
    "CONTROL_OPCODES", "DATA_OPCODES",
    "CLOSE_NORMAL", "CLOSE_GOING_AWAY", "CLOSE_PROTOCOL_ERROR",
    "CLOSE_UNSUPPORTED", "CLOSE_NO_STATUS", "CLOSE_ABNORMAL",
    "CLOSE_INVALID_PAYLOAD", "CLOSE_POLICY", "CLOSE_TOO_BIG",
    "CLOSE_MISSING_EXT", "CLOSE_INTERNAL",
    "MAX_CONTROL_PAYLOAD", "MAX_MESSAGE_BYTES",
    "ProtocolError", "Frame", "WSMessage", "FrameDecoder",
    "accept_key", "mask_bytes",
    "encode_frame", "encode_text", "encode_binary",
    "encode_close", "encode_ping", "encode_pong",
    "decode_frame", "parse_close", "drain_frames", "is_legal_close_code",
]

#: RFC 6455 §1.3 handshake GUID.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

CONTROL_OPCODES = frozenset({OP_CLOSE, OP_PING, OP_PONG})
DATA_OPCODES = frozenset({OP_CONT, OP_TEXT, OP_BINARY})
_KNOWN_OPCODES = CONTROL_OPCODES | DATA_OPCODES

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_UNSUPPORTED = 1003
CLOSE_NO_STATUS = 1005          # never sent on the wire (§7.4.1)
CLOSE_ABNORMAL = 1006           # never sent on the wire (§7.4.1)
CLOSE_INVALID_PAYLOAD = 1007
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_MISSING_EXT = 1010
CLOSE_INTERNAL = 1011

#: §5.5 — a control frame's payload is at most 125 bytes, full stop.
MAX_CONTROL_PAYLOAD = 125

#: Default cap on a reassembled data message. Touch's own frames are one JSONL
#: record each (kilobytes); 1 MiB is generous headroom that still bounds what a
#: single client can make the server buffer.
MAX_MESSAGE_BYTES = 1024 * 1024

#: Codes a peer may legally send (§7.4.1): 1000-1003, 1007-1011, plus the
#: registered (3000-3999) and private (4000-4999) ranges. 1004/1005/1006 and
#: anything below 1000 are reserved and must never appear on the wire.
_LEGAL_CLOSE_CODES = frozenset({1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011})


def is_legal_close_code(code) -> bool:
    """True for a code §7.4.1 allows on the wire, in **either** direction.

    One predicate, used by both :func:`encode_close` and :func:`parse_close`, so
    Touch can never emit a close frame its own decoder would fail the connection
    over (1004, 1012-2999 and everything below 1000 are reserved/undefined).
    """
    return isinstance(code, int) and not isinstance(code, bool) and (
        code in _LEGAL_CLOSE_CODES or 3000 <= code <= 4999
    )


class ProtocolError(Exception):
    """A framing/protocol violation, carrying the close code to send back."""

    def __init__(self, message, code=CLOSE_PROTOCOL_ERROR):
        super().__init__(message)
        self.code = code


def accept_key(client_key) -> str:
    """`Sec-WebSocket-Accept` for a `Sec-WebSocket-Key` (RFC 6455 §1.3).

    The RFC's own vector: `dGhlIHNhbXBsZSBub25jZQ==` →
    `s3pPLMBiTxaQ9kYGzzhZRbK+xOo=`.
    """
    if isinstance(client_key, bytes):
        client_key = client_key.decode("ascii", "strict")
    digest = hashlib.sha1((client_key.strip() + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def mask_bytes(data: bytes, key: bytes) -> bytes:
    """XOR ``data`` with the 4-byte ``key`` (§5.3). Masking is its own inverse."""
    if len(key) != 4:
        raise ValueError("masking key must be exactly 4 bytes")
    if not data:
        return b""
    # One big-int XOR beats a per-byte loop by ~50x on megabyte payloads and is
    # exact: the repeated key is truncated to the payload length.
    n = len(data)
    repeated = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(n, "big")


def encode_frame(payload: bytes = b"", opcode: int = OP_TEXT, *, fin: bool = True,
                 mask=None) -> bytes:
    """Serialize one frame.

    ``mask`` is ``None`` for a server frame (unmasked — §5.1), ``True`` to
    generate a fresh key, or an explicit 4-byte key (the RFC's vectors pin one,
    and so do the tests). Illegal *requests* raise ``ValueError``: asking for a
    fragmented ping is a bug in the caller, not a peer protocol error.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    payload = bytes(payload)
    if opcode not in _KNOWN_OPCODES:
        raise ValueError(f"unknown opcode 0x{opcode:X}")
    if opcode in CONTROL_OPCODES:
        if not fin:
            raise ValueError("control frames cannot be fragmented (RFC 6455 §5.5)")
        if len(payload) > MAX_CONTROL_PAYLOAD:
            raise ValueError(
                f"control frame payload is {len(payload)} bytes, max {MAX_CONTROL_PAYLOAD}"
            )
    if mask is True:
        mask = os.urandom(4)
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    n = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if n < 126:
        header.append(mask_bit | n)
    elif n < 65536:
        header.append(mask_bit | 126)
        header += n.to_bytes(2, "big")
    else:
        header.append(mask_bit | 127)
        header += n.to_bytes(8, "big")
    if mask:
        mask = bytes(mask)
        if len(mask) != 4:
            raise ValueError("masking key must be exactly 4 bytes")
        return bytes(header) + mask + mask_bytes(payload, mask)
    return bytes(header) + payload


def encode_text(text, **kwargs) -> bytes:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return encode_frame(text, OP_TEXT, **kwargs)


def encode_binary(data, **kwargs) -> bytes:
    return encode_frame(data, OP_BINARY, **kwargs)


def encode_ping(payload: bytes = b"", **kwargs) -> bytes:
    return encode_frame(payload, OP_PING, **kwargs)


def encode_pong(payload: bytes = b"", **kwargs) -> bytes:
    return encode_frame(payload, OP_PONG, **kwargs)


def encode_close(code: int = CLOSE_NORMAL, reason: str = "", **kwargs) -> bytes:
    """A close frame. ``code=None`` means "no status" — an empty payload (§5.5.1).

    The code must be one a peer may legally *receive*: exactly the predicate
    :func:`parse_close` enforces, so two Touch endpoints can never fail a
    connection over a close frame Touch itself generated. That bars 1005/1006
    **and** 1004 and the undefined 1012-2999 band (§7.4.1).
    """
    if code is None:
        return encode_frame(b"", OP_CLOSE, **kwargs)
    if not is_legal_close_code(code):
        raise ValueError(f"close code {code!r} may not be sent on the wire (RFC 6455 §7.4.1)")
    payload = code.to_bytes(2, "big") + reason.encode("utf-8")
    return encode_frame(payload, OP_CLOSE, **kwargs)


@dataclass(frozen=True)
class Frame:
    """One decoded frame, payload already unmasked."""

    fin: bool
    opcode: int
    payload: bytes
    masked: bool = False
    rsv: int = 0

    @property
    def is_control(self) -> bool:
        return self.opcode in CONTROL_OPCODES


@dataclass(frozen=True)
class WSMessage:
    """A complete application message, or one control frame.

    ``text`` is set only for a validated text message; ``data`` always holds the
    raw bytes, so a caller that wants the bytes of a text frame still has them.
    """

    opcode: int
    data: bytes
    text: str = None

    @property
    def is_text(self) -> bool:
        return self.opcode == OP_TEXT

    @property
    def is_control(self) -> bool:
        return self.opcode in CONTROL_OPCODES


def decode_frame(buf, offset: int = 0, *, require_mask=None,
                 max_frame_payload: int = MAX_MESSAGE_BYTES):
    """Decode one frame from ``buf[offset:]``.

    Returns ``(frame, consumed)``, or ``(None, 0)`` when the buffer does not yet
    hold a whole frame — the caller keeps the bytes and feeds more (a partial
    frame is normal on a stream socket, never an error).

    ``require_mask`` is ``True`` when decoding client→server frames (a server),
    ``False`` for server→client (a client), ``None`` to accept either — which
    only a test or a proxy should want.
    """
    view = memoryview(buf)[offset:]
    if len(view) < 2:
        return None, 0
    first, second = view[0], view[1]
    fin = bool(first & 0x80)
    rsv = (first >> 4) & 0x07
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    idx = 2
    if rsv:
        raise ProtocolError(f"RSV bits set (0x{rsv:X}) with no negotiated extension")
    if opcode not in _KNOWN_OPCODES:
        raise ProtocolError(f"unknown opcode 0x{opcode:X}")
    if opcode in CONTROL_OPCODES:
        if not fin:
            raise ProtocolError("fragmented control frame")
        if length > MAX_CONTROL_PAYLOAD:
            raise ProtocolError(f"control frame payload {length} > {MAX_CONTROL_PAYLOAD}")
    if length == 126:
        if len(view) < idx + 2:
            return None, 0
        length = int.from_bytes(view[idx:idx + 2], "big")
        idx += 2
        if length < 126:
            raise ProtocolError("length 126 used for a payload that fits in 7 bits")
    elif length == 127:
        if len(view) < idx + 8:
            return None, 0
        length = int.from_bytes(view[idx:idx + 8], "big")
        idx += 8
        if length >> 63:
            raise ProtocolError("64-bit length with the high bit set")
        if length < 65536:
            raise ProtocolError("length 127 used for a payload that fits in 16 bits")
    if require_mask is True and not masked:
        raise ProtocolError("client frame is not masked (RFC 6455 §5.1)")
    if require_mask is False and masked:
        raise ProtocolError("server frame is masked (RFC 6455 §5.1)")
    if max_frame_payload and length > max_frame_payload:
        # Refuse before allocating: the point of the cap is not to buffer it.
        raise ProtocolError(
            f"frame payload {length} > {max_frame_payload}", CLOSE_TOO_BIG
        )
    key = b""
    if masked:
        if len(view) < idx + 4:
            return None, 0
        key = bytes(view[idx:idx + 4])
        idx += 4
    if len(view) < idx + length:
        return None, 0
    payload = bytes(view[idx:idx + length])
    if masked:
        payload = mask_bytes(payload, key)
    return Frame(fin=fin, opcode=opcode, payload=payload, masked=masked, rsv=rsv), idx + length


def parse_close(payload: bytes):
    """``(code, reason)`` from a close frame payload (§5.5.1, §7.4.1).

    An empty payload is "no status received" — reported as
    ``(CLOSE_NO_STATUS, "")`` and never echoed back on the wire.
    """
    if not payload:
        return CLOSE_NO_STATUS, ""
    if len(payload) == 1:
        raise ProtocolError("close payload of 1 byte cannot hold a status code")
    code = int.from_bytes(payload[:2], "big")
    if not is_legal_close_code(code):
        raise ProtocolError(f"reserved/invalid close code {code}")
    try:
        reason = payload[2:].decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise ProtocolError("close reason is not valid UTF-8", CLOSE_INVALID_PAYLOAD) from None
    return code, reason


class FrameDecoder:
    """Incremental frame→message decoder with fragmentation reassembly.

    Feed it whatever the socket gave you; get back complete
    :class:`WSMessage`s. Control frames come through immediately even in the
    middle of a fragmented data message (§5.4), which is exactly why a
    ping-during-fragmentation test exists.
    """

    def __init__(self, role: str = "server", *,
                 max_message_bytes: int = MAX_MESSAGE_BYTES,
                 max_frame_payload: int = None):
        if role not in ("server", "client", "any"):
            raise ValueError("role must be 'server', 'client' or 'any'")
        self.role = role
        self.max_message_bytes = max_message_bytes
        self.max_frame_payload = max_frame_payload or max_message_bytes
        self._buf = bytearray()
        self._fragments = bytearray()
        self._frag_opcode = None
        self.closed = False
        self.close_code = None
        self.close_reason = ""
        self.frames_decoded = 0
        #: Bytes arriving after the CLOSE frame, dropped unprocessed (§5.5.1).
        #: Counted rather than ignored silently: it is the one number that says
        #: "this peer kept talking after saying goodbye".
        self.post_close_bytes = 0

    @property
    def require_mask(self):
        return {"server": True, "client": False, "any": None}[self.role]

    @property
    def buffered(self) -> int:
        """Bytes of an incomplete frame still held (a partial tail is normal)."""
        return len(self._buf)

    def feed(self, data: bytes) -> list:
        """Consume ``data``; return the messages it completed.

        Raises :class:`ProtocolError` on a violation — the caller sends
        ``encode_close(exc.code)`` and hangs up. State after a raise is
        undefined by design: a protocol-violating peer gets no second chance.

        Once a CLOSE frame has been seen, nothing further is processed (§5.5.1):
        remaining and subsequent bytes are dropped and counted in
        ``post_close_bytes``, so ``closed`` keeps meaning "this connection is
        over" instead of being a flag beside a stream of live data frames.
        """
        if self.closed:
            self.post_close_bytes += len(data)
            return []
        self._buf += data
        out = []
        while True:
            frame, consumed = decode_frame(
                self._buf, 0,
                require_mask=self.require_mask,
                max_frame_payload=self.max_frame_payload,
            )
            if frame is None:
                return out
            del self._buf[:consumed]
            self.frames_decoded += 1
            message = self._on_frame(frame)
            if message is not None:
                out.append(message)
            if self.closed:
                self.post_close_bytes += len(self._buf)
                del self._buf[:]
                return out

    def _on_frame(self, frame: Frame):
        if frame.is_control:
            if frame.opcode == OP_CLOSE:
                code, reason = parse_close(frame.payload)
                self.closed = True
                self.close_code = code
                self.close_reason = reason
            return WSMessage(opcode=frame.opcode, data=frame.payload)

        if frame.opcode == OP_CONT:
            if self._frag_opcode is None:
                raise ProtocolError("continuation frame with no message to continue")
            self._extend(frame.payload)
            if not frame.fin:
                return None
            opcode = self._frag_opcode
            payload = bytes(self._fragments)
            self._fragments = bytearray()
            self._frag_opcode = None
            return self._finish(opcode, payload)

        # OP_TEXT / OP_BINARY
        if self._frag_opcode is not None:
            raise ProtocolError("new data frame while a fragmented message is open")
        if frame.fin:
            return self._finish(frame.opcode, frame.payload)
        self._frag_opcode = frame.opcode
        self._fragments = bytearray()
        self._extend(frame.payload)
        return None

    def _extend(self, payload: bytes):
        if self.max_message_bytes and len(self._fragments) + len(payload) > self.max_message_bytes:
            raise ProtocolError(
                f"message exceeds {self.max_message_bytes} bytes", CLOSE_TOO_BIG
            )
        self._fragments += payload

    def _finish(self, opcode: int, payload: bytes):
        if self.max_message_bytes and len(payload) > self.max_message_bytes:
            raise ProtocolError(
                f"message exceeds {self.max_message_bytes} bytes", CLOSE_TOO_BIG
            )
        if opcode == OP_TEXT:
            try:
                text = payload.decode("utf-8", "strict")
            except UnicodeDecodeError:
                raise ProtocolError(
                    "text message is not valid UTF-8", CLOSE_INVALID_PAYLOAD
                ) from None
            return WSMessage(opcode=opcode, data=payload, text=text)
        return WSMessage(opcode=opcode, data=payload)


def drain_frames(buf: bytearray) -> bool:
    """Consume whole frames from ``buf`` in place, discarding payloads unread.

    Byte-compatible with `monitor_server.parse_client_frames` (the prior art):
    returns True if a CLOSE frame was seen, leaves an incomplete trailing frame
    in ``buf`` for the next read, and never allocates a payload. This is the
    right primitive for a **read-only** socket whose client frames are only
    pongs and closes; anything that needs the payload uses
    :class:`FrameDecoder`.

    Deliberately permissive: it is a drain, not a validator, so it does not
    police masking, RSV bits or opcodes. Its only job is to find the CLOSE that
    tells the writer task to stop.

    **The caller owns the read budget.** A 64-bit length header is honoured as
    written and is not capped here (prior-art parity is the requirement), so a
    peer that announces a huge frame simply leaves ``buf`` incomplete until that
    many bytes arrive — the growth is the caller's `recv` loop appending to
    ``buf``, and it is the caller that must bound it (a per-connection read
    budget, or :class:`FrameDecoder` with ``max_message_bytes``, which refuses
    an over-budget frame from the header).

    ``buf`` must be a ``bytearray``: consuming in place is the point, and a
    ``bytes`` argument would otherwise fail with an opaque "does not support item
    deletion" from the middle of the loop.
    """
    if not isinstance(buf, bytearray):
        raise TypeError(
            "drain_frames consumes buf in place; pass a bytearray, not "
            f"{type(buf).__name__}"
        )
    saw_close = False
    while len(buf) >= 2:
        opcode = buf[0] & 0x0F
        masked = buf[1] & 0x80
        length = buf[1] & 0x7F
        idx = 2
        if length == 126:
            if len(buf) < idx + 2:
                break
            length = int.from_bytes(buf[idx:idx + 2], "big")
            idx += 2
        elif length == 127:
            if len(buf) < idx + 8:
                break
            length = int.from_bytes(buf[idx:idx + 8], "big")
            idx += 8
        if masked:
            idx += 4
        if len(buf) < idx + length:
            break
        del buf[:idx + length]
        if opcode == OP_CLOSE:
            saw_close = True
    return saw_close
