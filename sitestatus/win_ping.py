"""ICMP ping on Windows via IcmpSendEcho (iphlpapi.dll).

Unlike shelling out to ping.exe, this needs no admin rights and is
independent of the system language. IPv4 only. All calls are blocking;
run through asyncio.to_thread.
"""

from __future__ import annotations

import socket
import struct
import time


def ping_round(address: str, count: int, timeout: int) -> dict:
    """Send `count` echo requests; returns the same dict shape as checks.icmp_ping."""
    try:
        import ctypes
        from ctypes import wintypes

        iphlpapi = ctypes.WinDLL("iphlpapi")
    except (ImportError, OSError, AttributeError) as e:
        return {"ok": False, "error": f"windows icmp unavailable: {e}"}

    class IP_OPTION_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Ttl", ctypes.c_ubyte),
            ("Tos", ctypes.c_ubyte),
            ("Flags", ctypes.c_ubyte),
            ("OptionsSize", ctypes.c_ubyte),
            ("OptionsData", ctypes.c_void_p),
        ]

    class ICMP_ECHO_REPLY(ctypes.Structure):
        _fields_ = [
            ("Address", ctypes.c_uint32),
            ("Status", ctypes.c_uint32),
            ("RoundTripTime", ctypes.c_uint32),
            ("DataSize", ctypes.c_ushort),
            ("Reserved", ctypes.c_ushort),
            ("Data", ctypes.c_void_p),
            ("Options", IP_OPTION_INFORMATION),
        ]

    IcmpCreateFile = iphlpapi.IcmpCreateFile
    IcmpCreateFile.restype = wintypes.HANDLE
    IcmpCloseHandle = iphlpapi.IcmpCloseHandle
    IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    IcmpSendEcho = iphlpapi.IcmpSendEcho
    IcmpSendEcho.restype = wintypes.DWORD
    IcmpSendEcho.argtypes = [
        wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p, wintypes.WORD,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    ]

    try:
        ip = socket.gethostbyname(address)
    except OSError as e:
        return {"ok": False, "error": f"cannot resolve {address}: {e}"}
    # IPAddr wants the 4 address bytes in network order, reinterpreted natively
    dest = struct.unpack("=I", socket.inet_aton(ip))[0]

    handle = IcmpCreateFile()
    if handle in (None, ctypes.c_void_p(-1).value):  # INVALID_HANDLE_VALUE
        return {"ok": False, "error": "IcmpCreateFile failed"}

    payload = b"sitestatus ping payload 32bytes!"
    reply_size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(payload) + 8
    reply_buf = ctypes.create_string_buffer(reply_size)
    rtts: list[float] = []
    try:
        for i in range(count):
            if i:
                time.sleep(0.25)
            start = time.perf_counter()
            n = IcmpSendEcho(handle, dest, payload, len(payload), None,
                             reply_buf, reply_size, int(timeout * 1000))
            measured = (time.perf_counter() - start) * 1000.0
            if n == 0:
                continue  # timeout or unreachable: counts as a lost packet
            reply = ctypes.cast(reply_buf, ctypes.POINTER(ICMP_ECHO_REPLY)).contents
            if reply.Status != 0:  # 0 = IP_SUCCESS
                continue
            rtt = float(reply.RoundTripTime)
            # RoundTripTime is whole milliseconds, so fast LAN replies read as
            # 0; the wall-clock measurement is a better sub-ms estimate
            rtts.append(rtt if rtt >= 1.0 else min(measured, 0.999))
    finally:
        IcmpCloseHandle(handle)

    loss = 100.0 * (count - len(rtts)) / count
    if not rtts:
        return {"ok": False, "loss_pct": 100.0, "error": "100% packet loss"}
    return {
        "ok": True,
        "loss_pct": loss,
        "rtt_min": min(rtts),
        "rtt_avg": sum(rtts) / len(rtts),
        "rtt_max": max(rtts),
    }
