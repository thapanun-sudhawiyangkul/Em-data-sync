"""
mdf_reader.py - read-only parser for SQL Server .MDF data files.

Reads the system catalog (sysschobjs, syscolpars, sysrowsets, sysallocunits)
directly from the page structure and then decodes the in-row data pages of
user tables. No SQL Server instance is needed.

Limitations (fine for small machine-log databases):
  * Only in-row data is decoded. LOB / row-overflow values come back as
    b'<offrow>' placeholders.
  * Columns added after table creation are assumed to keep colid order.
  * Only the primary data file is read (no secondary .ndf files).
"""
from __future__ import annotations

import struct
import uuid
from datetime import datetime, timedelta, date, time
from decimal import Decimal

PAGE_SIZE = 8192
HDR = 96

SYS_SCHOBJS = 34
SYS_COLPARS = 41
SYS_ROWSETS = 5
SYS_ALLOCUNITS = 7

# xtype -> (name, fixed length or None for variable, needs length from syscolpars)
FIXED_LEN = {
    36: 16,   # uniqueidentifier
    48: 1,    # tinyint
    52: 2,    # smallint
    56: 4,    # int
    58: 4,    # smalldatetime
    59: 4,    # real
    60: 8,    # money
    61: 8,    # datetime
    62: 8,    # float
    104: 1,   # bit (special: packed)
    122: 4,   # smallmoney
    127: 8,   # bigint
    189: 8,   # timestamp
    40: 3,    # date
}
LEN_FROM_META = {106, 108, 173, 175, 239, 41, 42, 43}  # decimal, numeric, binary, char, nchar, time, datetime2, datetimeoffset
VAR_TYPES = {34, 35, 98, 99, 165, 167, 231, 241, 240}  # image, text, sql_variant, ntext, varbinary, varchar, nvarchar, xml, clr

TYPE_NAMES = {
    34: "image", 35: "text", 36: "uniqueidentifier", 40: "date", 41: "time", 42: "datetime2",
    43: "datetimeoffset", 48: "tinyint", 52: "smallint", 56: "int", 58: "smalldatetime",
    59: "real", 60: "money", 61: "datetime", 62: "float", 98: "sql_variant", 99: "ntext",
    104: "bit", 106: "decimal", 108: "numeric", 122: "smallmoney", 127: "bigint",
    165: "varbinary", 167: "varchar", 173: "binary", 175: "char", 189: "timestamp",
    231: "nvarchar", 239: "nchar", 241: "xml", 240: "clr",
}

EPOCH_1900 = datetime(1900, 1, 1)
VARCHAR_CODEPAGE = "cp1252"   # code page of the (var)char columns; Pulse ReportDB uses CP1252


class Column:
    __slots__ = ("colid", "name", "xtype", "length", "prec", "scale", "is_var")

    def __init__(self, colid, name, xtype, length, prec, scale):
        self.colid = colid
        self.name = name
        self.xtype = xtype
        self.length = length
        self.prec = prec
        self.scale = scale
        self.is_var = xtype in VAR_TYPES

    @property
    def type_name(self):
        n = TYPE_NAMES.get(self.xtype, f"xtype{self.xtype}")
        if self.xtype in (167, 175, 165, 173):
            return f"{n}({'max' if self.length == -1 else self.length})"
        if self.xtype in (231, 239):
            return f"{n}({'max' if self.length == -1 else self.length // 2})"
        if self.xtype in (106, 108):
            return f"{n}({self.prec},{self.scale})"
        return n

    def fixed_size(self):
        if self.xtype in FIXED_LEN:
            return FIXED_LEN[self.xtype]
        if self.xtype in LEN_FROM_META:
            return self.length
        return None

    def sql_type(self):
        """T-SQL type string usable in CREATE TABLE on the destination server."""
        return self.type_name


class Table:
    def __init__(self, object_id, name, created, modified):
        self.object_id = object_id
        self.name = name
        self.created = created
        self.modified = modified
        self.columns: list[Column] = []
        self.auids: list[int] = []  # allocation units holding in-row data

    @property
    def fixed_columns(self):
        return [c for c in self.columns if c.fixed_size() is not None]

    @property
    def var_columns(self):
        return [c for c in self.columns if c.fixed_size() is None]


class MdfReader:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.data = f.read()
        self.page_count = len(self.data) // PAGE_SIZE
        self.tables: dict[int, Table] = {}
        self._load_catalog()

    # ------------------------------------------------------------------ pages
    def page(self, n):
        return self.data[n * PAGE_SIZE:(n + 1) * PAGE_SIZE]

    def page_header(self, pg):
        m_type = pg[1]
        m_indexId = struct.unpack_from("<H", pg, 6)[0]
        m_slotCnt = struct.unpack_from("<H", pg, 22)[0]
        m_objId = struct.unpack_from("<I", pg, 24)[0]
        auid = (m_indexId << 48) | (m_objId << 16)
        return m_type, auid, m_slotCnt

    def records(self, pg):
        """Yield raw record bytes for every live primary record on a data page."""
        m_type, auid, slot_cnt = self.page_header(pg)
        if m_type != 1:
            return
        for s in range(slot_cnt):
            off = struct.unpack_from("<H", pg, PAGE_SIZE - 2 * (s + 1))[0]
            if off == 0 or off >= PAGE_SIZE - 2 * slot_cnt:
                continue
            status_a = pg[off]
            rec_type = (status_a >> 1) & 0x07
            if rec_type not in (0, 1):      # primary or forwarded record only
                continue
            yield pg[off:]

    # ---------------------------------------------------------------- records
    @staticmethod
    def parse_record(rec, fixed_cols, var_cols):
        """Decode a record given its fixed and variable column lists.
        Returns dict name -> python value."""
        status_a = rec[0]
        has_null_bitmap = bool(status_a & 0x10)
        has_var = bool(status_a & 0x20)
        fixed_end = struct.unpack_from("<H", rec, 2)[0]
        pos = 4
        out = {}

        ncols = struct.unpack_from("<H", rec, fixed_end)[0] if has_null_bitmap else 0
        nb_len = (ncols + 7) // 8
        null_bitmap = rec[fixed_end + 2:fixed_end + 2 + nb_len] if has_null_bitmap else b""

        def is_null(colid):
            if not has_null_bitmap or colid > ncols:
                return False
            i = colid - 1
            return bool(null_bitmap[i // 8] >> (i % 8) & 1)

        bit_byte = None
        bit_idx = 8
        for c in fixed_cols:
            size = c.fixed_size()
            if c.xtype == 104:  # bit: packed 8 per byte, in fixed area
                if bit_idx == 8:
                    bit_byte = rec[pos]
                    pos += 1
                    bit_idx = 0
                val = bool(bit_byte >> bit_idx & 1)
                bit_idx += 1
                out[c.name] = None if is_null(c.colid) else val
                continue
            raw = rec[pos:pos + size]
            pos += size
            out[c.name] = None if is_null(c.colid) else decode_fixed(c, raw)

        if has_var and var_cols:
            p = fixed_end + 2 + nb_len
            var_cnt = struct.unpack_from("<H", rec, p)[0]
            p += 2
            offsets = [struct.unpack_from("<H", rec, p + 2 * i)[0] for i in range(var_cnt)]
            start = p + 2 * var_cnt
            for i, c in enumerate(var_cols):
                if is_null(c.colid) or i >= var_cnt:
                    out[c.name] = None
                    continue
                end = offsets[i] & 0x7FFF
                off_row = bool(offsets[i] & 0x8000)
                raw = rec[start:end]
                start = end
                out[c.name] = b"<offrow>" if off_row else decode_var(c, raw)
        else:
            for c in var_cols:
                out[c.name] = None
        return out

    # ---------------------------------------------------------------- catalog
    def _scan(self, sys_object_id):
        """Yield records of a core system table (its pages carry the object id
        directly in m_objId, with m_indexId 0 or 1)."""
        targets = {(0 << 48) | (sys_object_id << 16), (1 << 48) | (sys_object_id << 16)}
        for n in range(self.page_count):
            pg = self.page(n)
            m_type, auid, _ = self.page_header(pg)
            if m_type == 1 and auid in targets:
                yield from self.records(pg)

    def _load_catalog(self):
        # sysschobjs (id 34, index 1)
        schobjs_fixed = [
            Column(1, "id", 56, 4, 0, 0), Column(3, "nsid", 56, 4, 0, 0), Column(4, "nsclass", 48, 1, 0, 0),
            Column(5, "status", 56, 4, 0, 0), Column(6, "type", 175, 2, 0, 0), Column(7, "pid", 56, 4, 0, 0),
            Column(8, "pclass", 48, 1, 0, 0), Column(9, "intprop", 56, 4, 0, 0), Column(10, "created", 61, 8, 0, 0),
            Column(11, "modified", 61, 8, 0, 0), Column(12, "status2", 56, 4, 0, 0),
        ]
        schobjs_var = [Column(2, "name", 231, 256, 0, 0)]
        objs = {}
        for rec in self._scan(SYS_SCHOBJS):
            r = self.parse_record(rec, schobjs_fixed, schobjs_var)
            objs[r["id"]] = r
        user_tables = {oid: r for oid, r in objs.items() if r["type"].strip() == "U" and oid > 100}
        for oid, r in user_tables.items():
            self.tables[oid] = Table(oid, r["name"], r["created"], r["modified"])

        # syscolpars (id 41)
        colpars_fixed = [
            Column(1, "id", 56, 4, 0, 0), Column(2, "number", 52, 2, 0, 0), Column(3, "colid", 56, 4, 0, 0),
            Column(5, "xtype", 48, 1, 0, 0), Column(6, "utype", 56, 4, 0, 0), Column(7, "length", 52, 2, 0, 0),
            Column(8, "prec", 48, 1, 0, 0), Column(9, "scale", 48, 1, 0, 0), Column(10, "collationid", 56, 4, 0, 0),
            Column(11, "status", 56, 4, 0, 0), Column(12, "maxinrow", 52, 2, 0, 0), Column(13, "xmlns", 56, 4, 0, 0),
            Column(14, "dflt", 56, 4, 0, 0), Column(15, "chk", 56, 4, 0, 0),
        ]
        colpars_var = [Column(4, "name", 231, 256, 0, 0), Column(16, "idtval", 165, -1, 0, 0)]
        for rec in self._scan(SYS_COLPARS):
            r = self.parse_record(rec, colpars_fixed, colpars_var)
            t = self.tables.get(r["id"])
            if t is not None and r["number"] == 0:
                t.columns.append(Column(r["colid"], r["name"], r["xtype"], r["length"], r["prec"], r["scale"]))
        for t in self.tables.values():
            t.columns.sort(key=lambda c: c.colid)

        # sysrowsets (id 5): rowsetid -> (idmajor, idminor)
        rowsets_fixed = [
            Column(1, "rowsetid", 127, 8, 0, 0), Column(2, "ownertype", 48, 1, 0, 0), Column(3, "idmajor", 56, 4, 0, 0),
            Column(4, "idminor", 56, 4, 0, 0), Column(5, "numpart", 56, 4, 0, 0), Column(6, "status", 56, 4, 0, 0),
            Column(7, "fgidfs", 52, 2, 0, 0), Column(8, "rcrows", 127, 8, 0, 0),
        ]
        rowset_owner = {}
        for rec in self._scan(SYS_ROWSETS):
            r = self.parse_record(rec, rowsets_fixed, [])
            rowset_owner[r["rowsetid"]] = (r["idmajor"], r["idminor"])

        # sysallocunits (id 7): auid -> ownerid (rowsetid); type 1 = in-row data
        alloc_fixed = [
            Column(1, "auid", 127, 8, 0, 0), Column(2, "type", 48, 1, 0, 0), Column(3, "ownerid", 127, 8, 0, 0),
            Column(4, "status", 56, 4, 0, 0), Column(5, "fgid", 52, 2, 0, 0),
        ]
        for rec in self._scan(SYS_ALLOCUNITS):
            r = self.parse_record(rec, alloc_fixed, [])
            if r["type"] != 1:
                continue
            owner = rowset_owner.get(r["ownerid"])
            if owner is None:
                continue
            idmajor, idminor = owner
            t = self.tables.get(idmajor)
            if t is not None and idminor in (0, 1):   # heap or clustered index leaf
                t.auids.append(r["auid"])

    # ------------------------------------------------------------------- rows
    def rows(self, table: Table | str):
        if isinstance(table, str):
            table = next(t for t in self.tables.values() if t.name.lower() == table.lower())
        fixed, var = table.fixed_columns, table.var_columns
        auids = set(table.auids)
        for n in range(self.page_count):
            pg = self.page(n)
            m_type, auid, _ = self.page_header(pg)
            if m_type == 1 and auid in auids:
                for rec in self.records(pg):
                    yield self.parse_record(rec, fixed, var)

    def table(self, name) -> Table:
        return next(t for t in self.tables.values() if t.name.lower() == name.lower())


# ------------------------------------------------------------------ decoders
def decode_fixed(c: Column, raw: bytes):
    x = c.xtype
    try:
        if x == 56:
            return struct.unpack("<i", raw)[0]
        if x == 52:
            return struct.unpack("<h", raw)[0]
        if x == 48:
            return raw[0]
        if x == 127:
            return struct.unpack("<q", raw)[0]
        if x == 61:
            ticks, days = struct.unpack("<Ii", raw)
            return EPOCH_1900 + timedelta(days=days, milliseconds=round(ticks * 10 / 3))
        if x == 58:
            mins, days = struct.unpack("<HH", raw)
            return EPOCH_1900 + timedelta(days=days, minutes=mins)
        if x == 62:
            return struct.unpack("<d", raw)[0]
        if x == 59:
            return struct.unpack("<f", raw)[0]
        if x == 60:
            return Decimal(struct.unpack("<q", raw)[0]) / 10000
        if x == 122:
            return Decimal(struct.unpack("<i", raw)[0]) / 10000
        if x == 36:
            return uuid.UUID(bytes_le=raw)
        if x in (106, 108):
            sign = 1 if raw[0] == 1 else -1
            mag = int.from_bytes(raw[1:], "little")
            return Decimal(sign * mag).scaleb(-c.scale)
        if x == 175:
            return raw.decode(VARCHAR_CODEPAGE, errors="replace").rstrip(" ")
        if x == 239:
            return raw.decode("utf-16le").rstrip(" ")
        if x == 173:
            return raw
        if x == 189:
            return raw
        if x == 40:
            d = int.from_bytes(raw, "little")
            return date(1, 1, 1) + timedelta(days=d)
        if x == 42:  # datetime2: time part (scale-dependent) then 3-byte date
            tb = raw[:-3]
            d = int.from_bytes(raw[-3:], "little")
            t = int.from_bytes(tb, "little")
            secs = t / (10 ** c.scale)
            return datetime(1, 1, 1) + timedelta(days=d, seconds=secs)
        if x == 41:
            t = int.from_bytes(raw, "little")
            return (datetime(1, 1, 1) + timedelta(seconds=t / (10 ** c.scale))).time()
    except Exception:
        pass
    return raw


def decode_var(c: Column, raw: bytes):
    x = c.xtype
    if x == 231:
        return raw.decode("utf-16le", errors="replace")
    if x == 167:
        return raw.decode(VARCHAR_CODEPAGE, errors="replace")
    if x in (165, 34, 35, 99, 241, 98, 240):
        return raw
    return raw


if __name__ == "__main__":
    import sys
    r = MdfReader(sys.argv[1])
    for t in r.tables.values():
        cols = ", ".join(f"{c.name} {c.type_name}" for c in t.columns)
        n = sum(1 for _ in r.rows(t))
        print(f"{t.name} (id {t.object_id}, {n} rows, created {t.created}) : {cols}")
