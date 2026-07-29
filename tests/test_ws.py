#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/ws.py (R-29 / T3). Run as
`python3 test_ws.py`; exits non-zero on failure. No pytest, no runner.

R-29's test list is "RFC vectors, fragmented frames, masked client frames
dropped unread (matching monitor_server.py:279-310 behaviour)". Both halves are
here and they are not in tension: :func:`ws.drain_frames` is the prior art's
skip-and-discard path (asserted **against the real function** imported from
`monitor_server.py`, so the parity claim is checked rather than asserted), while
:class:`ws.FrameDecoder` is the unmasking codec T3 specifies for a socket that
will eventually carry control intents.

Every byte vector below is from RFC 6455 §5.7 (or §1.3 for the handshake), so a
failure here means Touch is off-spec, not that a test needs updating.
"""

import ast
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import MON, SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))

from aggregator import ws                                       # noqa: E402
from aggregator.ws import (                                     # noqa: E402
    CLOSE_INVALID_PAYLOAD,
    CLOSE_NO_STATUS,
    CLOSE_PROTOCOL_ERROR,
    CLOSE_TOO_BIG,
    MAX_CONTROL_PAYLOAD,
    OP_BINARY,
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    FrameDecoder,
    ProtocolError,
    accept_key,
    decode_frame,
    drain_frames,
    encode_binary,
    encode_close,
    encode_frame,
    encode_ping,
    encode_pong,
    encode_text,
    is_legal_close_code,
    mask_bytes,
    parse_close,
)

failures = []

# RFC 6455 §5.7 vectors.
V_TEXT_UNMASKED = bytes([0x81, 0x05, 0x48, 0x65, 0x6C, 0x6C, 0x6F])
V_TEXT_MASKED = bytes([0x81, 0x85, 0x37, 0xFA, 0x21, 0x3D,
                       0x7F, 0x9F, 0x4D, 0x51, 0x58])
V_FRAG_1 = bytes([0x01, 0x03, 0x48, 0x65, 0x6C])                # "Hel", fin=0
V_FRAG_2 = bytes([0x80, 0x02, 0x6C, 0x6F])                      # "lo",  fin=1, cont
V_PING = bytes([0x89, 0x05, 0x48, 0x65, 0x6C, 0x6C, 0x6F])
V_PONG = bytes([0x8A, 0x05, 0x48, 0x65, 0x6C, 0x6C, 0x6F])
V_BIN_256_HEADER = bytes([0x82, 0x7E, 0x01, 0x00])
V_BIN_64K_HEADER = bytes([0x82, 0x7F, 0, 0, 0, 0, 0, 0x01, 0x00, 0x00])


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def expect_close(code, fn, *args, **kwargs):
    """Assert ``fn`` raises ProtocolError carrying close code ``code``."""
    try:
        fn(*args, **kwargs)
    except ProtocolError as exc:
        if exc.code == code:
            return True
        print(f"    (got close {exc.code}, wanted {code}: {exc})")
        return False
    except Exception as other:
        print(f"    (raised {type(other).__name__}: {other})")
        return False
    return False


def feed(data, role="server", **kwargs):
    return FrameDecoder(role, **kwargs).feed(data)


# --- handshake ------------------------------------------------------------
def test_accept_key_rfc_vector():
    print("test_accept_key_rfc_vector")
    check(accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
          "RFC 6455 §1.3 accept-key vector")
    check(accept_key(b"dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
          "bytes input is accepted (headers arrive as bytes)")
    check(accept_key(" dGhlIHNhbXBsZSBub25jZQ== \r\n") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
          "surrounding header whitespace is stripped")


# --- encode: byte-exact against the RFC ----------------------------------
def test_encode_matches_rfc_vectors():
    print("test_encode_matches_rfc_vectors")
    check(encode_text("Hello") == V_TEXT_UNMASKED, "unmasked single-frame text vector")
    check(encode_text("Hello", mask=bytes([0x37, 0xFA, 0x21, 0x3D])) == V_TEXT_MASKED,
          "masked single-frame text vector (key from the RFC)")
    check(encode_frame(b"Hel", OP_TEXT, fin=False) == V_FRAG_1, "fragment 1 vector")
    check(encode_frame(b"lo", 0x0, fin=True) == V_FRAG_2, "fragment 2 (continuation) vector")
    check(encode_ping(b"Hello") == V_PING, "ping vector")
    check(encode_pong(b"Hello") == V_PONG, "pong vector")
    check(encode_binary(b"\x00" * 256)[:4] == V_BIN_256_HEADER,
          "256-byte binary uses the 16-bit length form")
    check(encode_binary(b"\x00" * 65536)[:10] == V_BIN_64K_HEADER,
          "65536-byte binary uses the 64-bit length form")
    check(encode_frame(b"") == bytes([0x81, 0x00]), "an empty text frame is two bytes")


def test_encode_rejects_illegal_requests():
    print("test_encode_rejects_illegal_requests")
    for fn, args, why in (
        (encode_frame, (b"x" * 126, OP_PING), "a control frame over 125 bytes"),
        (encode_frame, (b"", 0x3), "an unknown opcode"),
        (encode_close, (1005,), "close code 1005 (never on the wire)"),
        (encode_close, (1006,), "close code 1006 (never on the wire)"),
        (encode_close, (999,), "a close code below 1000"),
        # §7.4.1 reserves more than 1005/1006: our own parse_close rejects each
        # of these, so encoding one would let two Touch endpoints fail a
        # connection over a frame Touch itself generated.
        (encode_close, (1004,), "close code 1004 (reserved by §7.4.1)"),
        (encode_close, (1015,), "close code 1015 (reserved by §7.4.1)"),
        (encode_close, (1012,), "close code 1012 (undefined band 1012-2999)"),
        (encode_close, (2999,), "close code 2999 (undefined band 1012-2999)"),
        (encode_close, (5000,), "a close code above 4999"),
    ):
        try:
            fn(*args)
            check(False, f"should have raised ValueError for {why}")
        except ValueError:
            check(True, f"ValueError for {why}")
    try:
        encode_frame(b"", OP_PING, fin=False)
        check(False, "should have raised ValueError for a fragmented control frame")
    except ValueError:
        check(True, "ValueError for a fragmented control frame")
    check(len(encode_frame(b"x" * MAX_CONTROL_PAYLOAD, OP_PING)) == 127,
          "exactly 125 payload bytes in a control frame is legal")


def test_masking_is_its_own_inverse():
    print("test_masking_is_its_own_inverse")
    key = os.urandom(4)
    for size in (0, 1, 3, 4, 5, 1000, 65537):
        data = os.urandom(size)
        check(mask_bytes(mask_bytes(data, key), key) == data,
              f"mask(mask(x)) == x for {size} bytes")
    check(mask_bytes(b"Hello", bytes([0x37, 0xFA, 0x21, 0x3D])) ==
          bytes([0x7F, 0x9F, 0x4D, 0x51, 0x58]), "the RFC's masked payload bytes")
    try:
        mask_bytes(b"x", b"abc")
        check(False, "a 3-byte masking key should raise")
    except ValueError:
        check(True, "a masking key must be exactly 4 bytes")


# --- decode ---------------------------------------------------------------
def test_decode_rfc_vectors():
    print("test_decode_rfc_vectors")
    frame, used = decode_frame(V_TEXT_MASKED, require_mask=True)
    check(used == len(V_TEXT_MASKED) and frame.payload == b"Hello" and frame.masked,
          "a masked client frame is UNMASKED, not discarded (T3's departure from prior art)")
    frame, used = decode_frame(V_TEXT_UNMASKED, require_mask=False)
    check(frame.fin and frame.opcode == OP_TEXT and frame.payload == b"Hello",
          "an unmasked server frame decodes")
    frame, _ = decode_frame(V_FRAG_1, require_mask=False)
    check(frame.fin is False and frame.opcode == OP_TEXT, "fragment 1 has fin=0")
    check(decode_frame(V_TEXT_MASKED[:-1], require_mask=True) == (None, 0),
          "a partial frame returns (None, 0) — normal on a stream socket, not an error")
    check(decode_frame(b"", require_mask=True) == (None, 0), "an empty buffer is not an error")
    two = V_TEXT_UNMASKED + V_PING
    frame, used = decode_frame(two, require_mask=False)
    check(used == len(V_TEXT_UNMASKED), "decode consumes exactly one frame")
    frame2, _ = decode_frame(two, used, require_mask=False)
    check(frame2.opcode == OP_PING, "the offset argument decodes the next frame in place")


def test_decoder_reassembles_fragments():
    print("test_decoder_reassembles_fragments")
    msgs = feed(V_FRAG_1 + V_FRAG_2, role="client")
    check(len(msgs) == 1 and msgs[0].text == "Hello",
          "a two-frame fragmented message reassembles to one message")
    key = bytes([1, 2, 3, 4])
    three = (encode_frame(b"a", OP_TEXT, fin=False, mask=key)
             + encode_frame(b"b", 0x0, fin=False, mask=key)
             + encode_frame(b"c", 0x0, fin=True, mask=key))
    msgs = feed(three)
    check(len(msgs) == 1 and msgs[0].text == "abc", "a three-frame message reassembles")
    interleaved = (encode_frame(b"a", OP_TEXT, fin=False, mask=key)
                   + encode_ping(b"beat", mask=key)
                   + encode_frame(b"b", 0x0, fin=True, mask=key))
    msgs = feed(interleaved)
    check([m.opcode for m in msgs] == [OP_PING, OP_TEXT],
          "a ping interleaved into a fragmented message is delivered immediately (§5.4)")
    check(msgs[1].text == "ab", "the interleaved control frame does not corrupt the message")


def test_decoder_streams_byte_by_byte():
    print("test_decoder_streams_byte_by_byte")
    key = bytes([9, 8, 7, 6])
    wire = (encode_text("x" * 300, mask=key) + encode_ping(b"", mask=key)
            + encode_close(1000, "bye", mask=key))
    dec = FrameDecoder("server")
    got = []
    for i in range(len(wire)):
        got.extend(dec.feed(wire[i:i + 1]))
    check([m.opcode for m in got] == [OP_TEXT, OP_PING, OP_CLOSE],
          "feeding one byte at a time yields the same three messages")
    check(got[0].text == "x" * 300, "the 16-bit-length message survives byte-wise feeding")
    check(dec.closed and dec.close_code == 1000 and dec.close_reason == "bye",
          "the close frame's code and reason are recorded")
    check(dec.buffered == 0, "nothing is left buffered")


def test_nothing_is_processed_after_a_close():
    print("test_nothing_is_processed_after_a_close")
    # §5.5.1: after a close frame nothing further is processed. Continuing to
    # decode data frames would leave `closed` a flag beside live traffic.
    key = bytes([7, 7, 7, 7])
    wire = (encode_close(1000, "bye", mask=key)
            + encode_text("after the close", mask=key))
    dec = FrameDecoder("server")
    msgs = dec.feed(wire)
    check([m.opcode for m in msgs] == [OP_CLOSE],
          "the close is delivered and the frame behind it is not")
    check(dec.closed and dec.close_code == 1000 and dec.buffered == 0,
          "closed is recorded and no post-close bytes stay buffered")
    check(dec.post_close_bytes == len(encode_text("after the close", mask=key)),
          "the dropped bytes are counted, not silently swallowed")
    check(dec.feed(encode_text("more", mask=key)) == [],
          "a later feed() on a closed decoder yields nothing at all")
    check(dec.post_close_bytes > len(encode_text("after the close", mask=key)),
          "the counter keeps growing while the peer keeps talking")
    check(dec.frames_decoded == 1, "no frame after the close is even decoded")


def test_close_frames():
    print("test_close_frames")
    check(parse_close(b"") == (CLOSE_NO_STATUS, ""),
          "an empty close payload is 'no status received' (1005), never echoed")
    check(parse_close(encode_close(1001, "going")[2:]) == (1001, "going"),
          "code + reason round-trip")
    check(expect_close(CLOSE_PROTOCOL_ERROR, parse_close, b"\x03"),
          "a 1-byte close payload is a protocol error")
    for code in (999, 1004, 1005, 1006, 1015, 2999, 5000):
        check(expect_close(CLOSE_PROTOCOL_ERROR, parse_close,
                           code.to_bytes(2, "big")),
              f"reserved/invalid close code {code} is rejected")
    for code in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 3000, 4999):
        check(parse_close(code.to_bytes(2, "big"))[0] == code, f"close code {code} is legal")
    check(expect_close(CLOSE_INVALID_PAYLOAD, parse_close, b"\x03\xe8\xff\xfe"),
          "a close reason that is not UTF-8 is 1007")
    check(encode_close(None) == bytes([0x88, 0x00]), "code=None encodes an empty close frame")

    # One predicate, both directions: everything we can send, we can read, and
    # nothing we reject can be produced by our own encoder.
    sendable, readable = [], []
    for code in list(range(995, 1020)) + [2999, 3000, 3999, 4000, 4999, 5000]:
        try:
            encode_close(code)
            sendable.append(code)
        except ValueError:
            pass
        try:
            parse_close(code.to_bytes(2, "big"))
            readable.append(code)
        except ProtocolError:
            pass
    check(sendable == readable,
          f"encode_close and parse_close agree on §7.4.1 exactly: {sendable}")
    check(all(is_legal_close_code(c) for c in sendable)
          and not is_legal_close_code(1004) and not is_legal_close_code(True),
          "is_legal_close_code is that shared predicate (and a bool is not a code)")


def test_protocol_violations_map_to_close_codes():
    print("test_protocol_violations_map_to_close_codes")
    key = bytes([1, 1, 1, 1])
    cases = [
        (CLOSE_PROTOCOL_ERROR, bytes([0xC1, 0x80, 1, 1, 1, 1]), "RSV1 set"),
        (CLOSE_PROTOCOL_ERROR, bytes([0xA1, 0x80, 1, 1, 1, 1]), "RSV2 set"),
        (CLOSE_PROTOCOL_ERROR, bytes([0x91, 0x80, 1, 1, 1, 1]), "RSV3 set"),
        (CLOSE_PROTOCOL_ERROR, bytes([0x83, 0x80, 1, 1, 1, 1]), "unknown opcode 0x3"),
        (CLOSE_PROTOCOL_ERROR, bytes([0x8B, 0x80, 1, 1, 1, 1]), "unknown control opcode 0xB"),
        (CLOSE_PROTOCOL_ERROR, bytes([0x09, 0x80, 1, 1, 1, 1]), "fragmented ping"),
        (CLOSE_PROTOCOL_ERROR, bytes([0x89, 0xFE, 0x00, 0x7E]) + b"\x00" * 4,
         "control frame payload > 125"),
        (CLOSE_PROTOCOL_ERROR, V_TEXT_UNMASKED, "unmasked client frame (server role)"),
        (CLOSE_PROTOCOL_ERROR, bytes([0x81, 0xFE, 0x00, 0x05]) + b"\x00" * 4 + b"Hello",
         "non-minimal 16-bit length"),
        (CLOSE_PROTOCOL_ERROR,
         bytes([0x81, 0xFF]) + (100).to_bytes(8, "big") + b"\x00" * 4 + b"x" * 100,
         "non-minimal 64-bit length"),
        (CLOSE_PROTOCOL_ERROR,
         bytes([0x81, 0xFF]) + (1 << 63).to_bytes(8, "big") + b"\x00" * 4,
         "64-bit length with the high bit set"),
    ]
    for code, wire, why in cases:
        check(expect_close(code, feed, wire), f"{why} => close {code}")

    check(expect_close(CLOSE_PROTOCOL_ERROR, feed, encode_text("x", mask=key), "client"),
          "a masked SERVER frame is a violation for a client-role decoder")
    check(expect_close(CLOSE_PROTOCOL_ERROR, feed,
                       encode_frame(b"x", 0x0, fin=True, mask=key)),
          "a continuation with no message to continue => 1002")
    check(expect_close(CLOSE_PROTOCOL_ERROR, feed,
                       encode_frame(b"a", OP_TEXT, fin=False, mask=key)
                       + encode_frame(b"b", OP_TEXT, fin=True, mask=key)),
          "a new data frame while a fragmented message is open => 1002")
    check(expect_close(CLOSE_INVALID_PAYLOAD, feed,
                       encode_frame(b"\xff\xfe", OP_TEXT, mask=key)),
          "invalid UTF-8 in a text message => 1007")
    check(feed(encode_frame(b"\xff\xfe", OP_BINARY, mask=key))[0].data == b"\xff\xfe",
          "the same bytes in a BINARY message are fine (no UTF-8 rule there)")


def test_size_caps():
    print("test_size_caps")
    key = bytes([2, 2, 2, 2])
    cap = 1000
    exact = feed(encode_text("y" * cap, mask=key), max_message_bytes=cap)
    check(len(exact) == 1 and len(exact[0].data) == cap, "a message exactly at the cap passes")
    check(expect_close(CLOSE_TOO_BIG, feed, encode_text("y" * (cap + 1), mask=key),
                       max_message_bytes=cap),
          "one byte over the cap => close 1009")
    frames = (encode_frame(b"y" * 600, OP_TEXT, fin=False, mask=key)
              + encode_frame(b"y" * 600, 0x0, fin=True, mask=key))
    check(expect_close(CLOSE_TOO_BIG, feed, frames, max_message_bytes=cap),
          "fragments that together exceed the cap => 1009 (checked while accumulating)")
    check(expect_close(CLOSE_TOO_BIG, decode_frame,
                       bytes([0x81, 0xFE, 0x10, 0x00]) + b"\x00" * 4,
                       require_mask=True, max_frame_payload=100),
          "an oversize frame is refused from its header, before the payload is buffered")


# --- prior-art parity: the skip-and-discard drain -------------------------
def test_drain_frames_parity_with_monitor_server():
    print("test_drain_frames_parity_with_monitor_server")
    key = bytes([4, 3, 2, 1])
    wire = (encode_pong(b"", mask=key) + encode_text("x" * 130, mask=key)
            + encode_text("ignored payload", mask=key) + encode_close(1000, "", mask=key))
    buf = bytearray(wire)
    check(drain_frames(buf) is True, "drain_frames reports the CLOSE frame")
    check(len(buf) == 0, "every whole frame is consumed in place")
    partial = bytearray(encode_pong(b"", mask=key) + wire[:3])
    check(drain_frames(partial) is False, "no CLOSE => False")
    check(len(partial) == 3, "an incomplete trailing frame is left for the next read")
    try:
        drain_frames(bytes(wire))
        check(False, "drain_frames should refuse an immutable bytes buffer")
    except TypeError:
        check(True, "drain_frames states its in-place contract (TypeError on bytes)")

    entry = str(MON)
    sys.path.insert(0, entry)
    # `monitor_server.py` resolves a task state dir AT IMPORT and exits (rc 2)
    # when it finds none — the clean-checkout case (`git archive HEAD | tar -x`
    # carries no `.claude/local-orchestrators/`). Handing it a scratch dir keeps
    # this arm RUNNING there instead of skipping: it is the only place
    # `drain_frames` is checked against the prior art's `parse_client_frames`,
    # so a skip would leave the parity claim unchecked at the release gate
    # (GD-C7's clean-checkout run). The import writes nothing into the scratch
    # dir, and `parse_client_frames` is pure.
    prior_state_dir = os.environ.get("ORCH_STATE_DIR")
    scratch_state_dir = tempfile.TemporaryDirectory(prefix="touch-ws-parity-")
    os.environ["ORCH_STATE_DIR"] = scratch_state_dir.name
    try:
        import monitor_server                                    # noqa: E402
    # `SystemExit` is a BaseException and sails past `except Exception`, and a
    # genuinely broken module still exits. Catching both is the backstop that
    # keeps `run_all.sh`'s promise — "files that read the absent things SKIP
    # there; nothing crashes" (GD-C7) — instead of exiting this file rc=1. The
    # line starts with SKIP so the runner COUNTS it (`run_all.sh`: skip/SKIP
    # first on the line); a silently-not-run arm is the failure mode that
    # promise exists to prevent.
    except (Exception, SystemExit) as exc:                       # pragma: no cover
        print(f"    SKIP: prior-art parity — monitor_server not importable "
              f"({type(exc).__name__}: {exc})")
        return
    finally:
        # Do not leave the monitoring dir on sys.path for the rest of the
        # process: it would shadow any future Touch module sharing a name there.
        if entry in sys.path:
            sys.path.remove(entry)
        if prior_state_dir is None:
            os.environ.pop("ORCH_STATE_DIR", None)
        else:
            os.environ["ORCH_STATE_DIR"] = prior_state_dir
        scratch_state_dir.cleanup()
    for sample in (wire, wire[:-2], b"", encode_ping(b"hi", mask=key)):
        mine, theirs = bytearray(sample), bytearray(sample)
        check(drain_frames(mine) == monitor_server.parse_client_frames(theirs)
              and bytes(mine) == bytes(theirs),
              f"drain_frames matches monitor_server.parse_client_frames on {len(sample)} bytes")


def test_frame_and_message_helpers():
    print("test_frame_and_message_helpers")
    frame, _ = decode_frame(V_PING, require_mask=False)
    check(frame.is_control, "Frame.is_control is True for a ping")
    pong, _ = decode_frame(V_PONG, require_mask=False)
    check(pong.opcode == OP_PONG and pong.payload == b"Hello" and pong.is_control,
          "the pong vector decodes to a control frame with its payload intact")
    msg = feed(encode_text("hi", mask=bytes([5, 5, 5, 5])))[0]
    check(msg.is_text and msg.text == "hi" and msg.data == b"hi",
          "a text message exposes both str and bytes")
    check(not msg.is_control, "a data message is not a control message")
    dec = FrameDecoder("any")
    check(dec.require_mask is None, "role 'any' accepts either masking direction")
    check(len(dec.feed(V_TEXT_UNMASKED)) == 1 and dec.frames_decoded == 1,
          "role 'any' decodes an unmasked frame and counts it")
    try:
        FrameDecoder("proxy")
        check(False, "an unknown role should raise")
    except ValueError:
        check(True, "an unknown role raises ValueError")


def test_module_does_no_io():
    print("test_module_does_no_io")
    # "No socket I/O in this module" (T3) asserted on the import graph and the
    # call graph, not on prose: the docstring legitimately talks about sockets.
    src = (SRC / "aggregator" / "ws.py").read_text()
    tree = ast.parse(src)
    imported = set()
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)
    check(imported == {"__future__", "base64", "hashlib", "os", "dataclasses"},
          f"imports are exactly the five stdlib primitives it needs: {sorted(imported)}")
    for forbidden in ("socket", "asyncio", "select", "ssl", "urllib", "http"):
        check(forbidden not in imported, f"ws.py does not import {forbidden}")
    for forbidden in ("open", "print", "input", "exec", "eval"):
        check(forbidden not in calls, f"ws.py never calls {forbidden}()")
    check(ws.MAX_CONTROL_PAYLOAD == 125, "the control-frame cap is the RFC's, not a choice")


def main():
    for t in (test_accept_key_rfc_vector, test_encode_matches_rfc_vectors,
              test_encode_rejects_illegal_requests, test_masking_is_its_own_inverse,
              test_decode_rfc_vectors, test_decoder_reassembles_fragments,
              test_decoder_streams_byte_by_byte, test_nothing_is_processed_after_a_close,
              test_close_frames,
              test_protocol_violations_map_to_close_codes, test_size_caps,
              test_drain_frames_parity_with_monitor_server,
              test_frame_and_message_helpers, test_module_does_no_io):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all ws codec tests passed")


if __name__ == "__main__":
    main()
