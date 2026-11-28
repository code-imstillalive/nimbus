"""Minimal GRIB2 reader (numpy only) for one field on a regular lat/lon grid.

Supports grid templates 3.0 (regular lat/lon: NOAA GFS / GEFS, ECMWF open data) and 3.101 (DWD ICON's
unstructured icosahedral grid), and data templates 5.0 (simple packing), 5.3 (complex packing with spatial
differencing) and 5.42 (CCSDS / AEC, as ECMWF and DWD use), no bitmap. Enough to read DSWRF / TMP / cloud at a
point without ecCodes (which cannot be installed inside Home Assistant).
"""

from __future__ import annotations

import struct

import numpy as np


def _sm(raw: int, nbytes: int) -> int:
    """GRIB sign-magnitude integer."""
    top = 1 << (8 * nbytes - 1)
    return -(raw & (top - 1)) if raw & top else raw


def _ieee(b: bytes) -> float:
    return struct.unpack(">f", b)[0]


def _bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def _read_fixed(bits, pos, n, w):
    """n unsigned integers of w bits each starting at bit pos -> (values, new pos)."""
    if w == 0 or n == 0:
        return np.zeros(n, dtype=np.int64), pos
    seg = bits[pos : pos + n * w].reshape(n, w).astype(np.int64)
    vals = seg @ (1 << np.arange(w - 1, -1, -1, dtype=np.int64))
    return vals, pos + n * w


def _sections(msg: bytes):
    out = {}
    i = 16
    while i < len(msg) - 4 and msg[i : i + 4] != b"7777":
        ln = struct.unpack(">I", msg[i : i + 4])[0]
        out[msg[i + 4]] = msg[i : i + ln]
        i += ln
    return out


def decode(msg: bytes, point=None, upto=None):
    """Return (values 2-D [lat, lon] float array, lats 1-D, lons 1-D).

    Unstructured grids (template 3.101, e.g. DWD ICON's triangles) return (1-D values, None, None); `upto` then stops
    decoding after that many points (the site's cell index is all that is needed)."""
    if msg[:4] != b"GRIB" or msg[7] != 2:
        raise ValueError("not a GRIB2 message")
    s = _sections(msg)
    s3, s5, s6, s7 = s[3], s[5], s.get(6), s[7]
    tpl3 = struct.unpack(">H", s3[12:14])[0]
    if tpl3 == 101:
        return _decode_values(s, upto), None, None
    if tpl3 != 0:
        raise ValueError(f"grid template {tpl3} not supported")
    ni = struct.unpack(">I", s3[30:34])[0]
    nj = struct.unpack(">I", s3[34:38])[0]
    la1 = _sm(struct.unpack(">I", s3[46:50])[0], 4) / 1e6
    lo1 = _sm(struct.unpack(">I", s3[50:54])[0], 4) / 1e6
    la2 = _sm(struct.unpack(">I", s3[55:59])[0], 4) / 1e6
    lo2 = _sm(struct.unpack(">I", s3[59:63])[0], 4) / 1e6
    if s6 is not None and s6[5] != 255:
        raise ValueError("bitmaps not supported")
    npts = struct.unpack(">I", s5[5:9])[0]
    tpl5 = struct.unpack(">H", s5[9:11])[0]
    R = _ieee(s5[11:15])
    E = _sm(struct.unpack(">H", s5[15:17])[0], 2)
    D = _sm(struct.unpack(">H", s5[17:19])[0], 2)
    nbits = s5[19]
    data = s7[5:]
    bits = _bits(data)
    if tpl5 == 0:
        x, _ = _read_fixed(bits, 0, npts, nbits)
        x = x.astype(float)
    elif tpl5 == 3:
        ng = struct.unpack(">I", s5[31:35])[0]
        ref_w = s5[35]
        bits_w = s5[36]
        ref_l = struct.unpack(">I", s5[37:41])[0]
        inc_l = s5[41]
        last_l = struct.unpack(">I", s5[42:46])[0]
        bits_l = s5[46]
        order = s5[47]
        nd = s5[48]
        pos = 0

        def take_sm():
            nonlocal pos
            raw = int.from_bytes(data[pos // 8 : pos // 8 + nd], "big")
            pos += 8 * nd
            return _sm(raw, nd)

        h = [take_sm() for _ in range(order)]
        hmin = take_sm()
        grp_ref, pos = _read_fixed(bits, pos, ng, nbits)
        pos = (pos + 7) // 8 * 8
        grp_w, pos = _read_fixed(bits, pos, ng, bits_w)
        grp_w = grp_w + ref_w
        pos = (pos + 7) // 8 * 8
        grp_l, pos = _read_fixed(bits, pos, ng, bits_l)
        grp_l = ref_l + inc_l * grp_l
        grp_l[-1] = last_l
        pos = (pos + 7) // 8 * 8
        # packed values: group g has grp_l[g] values of grp_w[g] bits each
        counts = grp_l.astype(np.int64)
        widths = np.repeat(grp_w.astype(np.int64), counts)
        starts = pos + np.concatenate([[0], np.cumsum(widths)[:-1]])
        vals = np.zeros(len(widths), dtype=np.int64)
        for k in range(int(widths.max()) if len(widths) else 0):
            m = k < widths
            vals[m] = vals[m] * 2 + bits[starts[m] + k]
        d = vals + np.repeat(grp_ref.astype(np.int64), counts)
        # undo spatial differencing
        d = d + hmin
        x = np.empty(npts, dtype=np.float64)
        if order == 1:
            d[0] = h[0]
            x = np.cumsum(d).astype(float)
        elif order == 2:
            d[0], d[1] = h[0], h[1]
            # X[i] = d[i] + 2X[i-1] - X[i-2]  ==> first differences are cumulative sums of d[2:] seeded by X1-X0
            first = np.empty(npts, dtype=np.int64)
            first[0] = h[0]
            first[1] = h[1] - h[0]
            first[2:] = d[2:]
            diff1 = np.cumsum(first[1:])
            x = np.concatenate([[h[0]], h[0] + np.cumsum(diff1)]).astype(float)
        else:
            raise ValueError(f"spatial differencing order {order} not supported")
    elif tpl5 == 42:
        flags, bs, rsi = s5[21], s5[22], struct.unpack(">H", s5[23:25])[0]
        stop = None
        if point is not None:  # only rows down to just past the point are needed
            dlat = (la2 - la1) / (nj - 1)
            row = int(np.floor((point[0] - la1) / dlat)) + 2
            stop = min(npts, row * ni)
        x = _aec_decode(data, nbits, flags, bs, rsi, npts, stop).astype(float)
        if len(x) < npts:
            x = np.concatenate([x, np.full(npts - len(x), np.nan)])
    else:
        raise ValueError(f"data template {tpl5} not supported")
    vals = (R + x * 2.0**E) / 10.0**D
    lats = np.linspace(la1, la2, nj)
    lons = lo1 + np.arange(ni) * (((lo2 - lo1) % 360) or 360) / (ni - 1)
    return vals.reshape(nj, ni), lats, lons


def at_point(field, lats, lons, lat, lon):
    """Bilinear value at (lat, lon)."""
    fi = (lat - lats[0]) / (lats[1] - lats[0])
    fj = ((lon - lons[0]) % 360) / (
        lons[1] - lons[0]
    )  # grids starting at 0, -180 or 180 all work
    i0, j0 = int(np.floor(fi)), int(np.floor(fj))
    di, dj = fi - i0, fj - j0
    f = field
    j1 = (j0 + 1) % f.shape[1]
    return float(
        (1 - di) * (1 - dj) * f[i0, j0]
        + (1 - di) * dj * f[i0, j1]
        + di * (1 - dj) * f[i0 + 1, j0]
        + di * dj * f[i0 + 1, j1]
    )


def _aec_decode(
    data: bytes,
    nbits: int,
    flags: int,
    bs: int,
    rsi: int,
    nsamples: int,
    stop_at: int | None = None,
):
    """CCSDS 121.0-B adaptive entropy decoding (what libaec does), numpy-assisted, for GRIB data template 5.42.

    Decodes unsigned samples of `nbits` with the unit-delay predictor (flag 8) and optional RSI byte padding (flag 32).
    Stops early once `stop_at` samples are out (only the rows down to a point are needed)."""
    if flags & 1:
        raise ValueError("signed AEC samples not supported")
    pre, pad = bool(flags & 8), bool(flags & 32)
    restricted = bool(flags & 16)
    if nbits > 16:
        id_len = 5
    elif nbits > 8:
        id_len = 4
    elif restricted and nbits <= 2:
        id_len = 1
    elif restricted and nbits <= 4:
        id_len = 2
    else:
        id_len = 3
    bits = _bits(data)
    ones = np.flatnonzero(bits)
    nb = len(bits)
    w_id = 1 << np.arange(id_len - 1, -1, -1)
    out = np.empty(nsamples + bs * rsi, dtype=np.int64)
    n_out = 0
    pos = 0
    need = nsamples if stop_at is None else min(nsamples, stop_at)
    xmax = (1 << nbits) - 1
    med = xmax // 2 + 1
    unc = (1 << id_len) - 1

    def rd(p, w):
        return int(bits[p : p + w] @ (1 << np.arange(w - 1, -1, -1))) if w else 0

    def unary(p):
        j = np.searchsorted(ones, p)
        e = int(ones[j])
        return e - p, e + 1

    while n_out < need and pos < nb - id_len:
        rsi_start = n_out
        b = 0
        while b < rsi and n_out < need and pos < nb - id_len:
            oid = int(bits[pos : pos + id_len] @ w_id)
            pos += id_len
            ref = pre and b == 0
            refv = 0
            if oid == 0:
                low = int(bits[pos])
                pos += 1
                if ref:
                    refv = rd(pos, nbits)
                    pos += nbits
                if low == 0:  # run of zero blocks
                    fs, pos = unary(pos)
                    z = fs + 1
                    if z == 5:  # remainder of the 64-block segment
                        z = min(rsi - b, 64 - (b % 64))
                    elif z > 5:
                        z -= 1
                    cnt = z * bs
                    out[n_out : n_out + cnt] = 0
                    if ref:
                        out[n_out] = refv
                    n_out += cnt
                    b += z
                    continue
                # second extension: each pair of samples is one unary number
                i = 1 if ref else 0
                vals = [refv] if ref else []
                while i < bs:
                    m, pos = unary(pos)
                    beta = int((np.sqrt(8 * m + 1) - 1) // 2)
                    while beta * (beta + 1) // 2 > m:
                        beta -= 1
                    while (beta + 1) * (beta + 2) // 2 <= m:
                        beta += 1
                    d1 = m - beta * (beta + 1) // 2
                    if i % 2 == 0:
                        vals.append(beta - d1)
                        i += 1
                    vals.append(d1)
                    i += 1
                out[n_out : n_out + bs] = vals[:bs]
                n_out += bs
                b += 1
                continue
            if ref:
                refv = rd(pos, nbits)
                pos += nbits
            cnt = bs - 1 if ref else bs
            if oid == unc:  # stored as is
                v = bits[pos : pos + cnt * nbits].reshape(cnt, nbits).astype(
                    np.int64
                ) @ (1 << np.arange(nbits - 1, -1, -1))
                pos += cnt * nbits
            else:  # split: all unary high parts, then all k-bit low parts
                k = oid - 1
                j = np.searchsorted(ones, pos)
                ends = ones[j : j + cnt].astype(np.int64)
                starts = np.concatenate([[pos], ends[:-1] + 1])
                fs = ends - starts
                pos = int(ends[-1]) + 1
                if k:
                    lsb = bits[pos : pos + cnt * k].reshape(cnt, k).astype(np.int64) @ (
                        1 << np.arange(k - 1, -1, -1)
                    )
                    pos += cnt * k
                    v = (fs << k) | lsb
                else:
                    v = fs
            if ref:
                out[n_out] = refv
                out[n_out + 1 : n_out + bs] = v
            else:
                out[n_out : n_out + bs] = v
            n_out += bs
            b += 1
        if pre:  # undo the unit-delay predictor over this RSI
            seg = out[rsi_start:n_out].tolist()
            x = seg[0]
            for q in range(1, len(seg)):
                d = seg[q]
                half = (d >> 1) + (d & 1)
                if x < med:
                    if half <= x:
                        x = x - half if d & 1 else x + half
                    else:
                        x = d
                else:
                    if half <= xmax - x:
                        x = x - half if d & 1 else x + half
                    else:
                        x = xmax - d
                seg[q] = x
            out[rsi_start:n_out] = seg
        if pad:
            pos = (pos + 7) // 8 * 8
    return out[: min(n_out, nsamples)]


def _decode_values(s, upto=None):
    """Values only (no grid), for unstructured grids."""
    s5, s6, s7 = s[5], s.get(6), s[7]
    if s6 is not None and s6[5] != 255:
        raise ValueError("bitmaps not supported")
    npts = struct.unpack(">I", s5[5:9])[0]
    tpl5 = struct.unpack(">H", s5[9:11])[0]
    R = _ieee(s5[11:15])
    E = _sm(struct.unpack(">H", s5[15:17])[0], 2)
    D = _sm(struct.unpack(">H", s5[17:19])[0], 2)
    nbits = s5[19]
    data = s7[5:]
    if tpl5 == 42:
        x = _aec_decode(
            data, nbits, s5[21], s5[22], struct.unpack(">H", s5[23:25])[0], npts, upto
        ).astype(float)
    elif tpl5 == 0:
        n = npts if upto is None else min(npts, upto)
        x, _ = _read_fixed(_bits(data), 0, n, nbits)
        x = x.astype(float)
    else:
        raise ValueError(f"data template {tpl5} not supported")
    return (R + x * 2.0**E) / 10.0**D
