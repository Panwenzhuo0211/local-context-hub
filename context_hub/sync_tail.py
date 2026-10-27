"""Verified rollout tails. Cache metadata/hash state, never entire histories.

An unchanged file needs no full read. Any external change revalidates the
consumed prefix before a tail can be delivered. Only complete JSONL records
are eligible; a partial UTF-8/JSON line stays at the source cursor.
"""
import hashlib
import json
import os
from dataclasses import dataclass


def token(stat):
    # Windows 3.14 path.stat ctime is birth time while fstat ctime is write
    # time. Compare identity/size/mtime consistently on paths and handles.
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


@dataclass
class Tail:
    offset: int
    prefix: str
    seal: tuple
    hasher: object
    ordinal: int
    data: bytes

    def digest_at(self, end):
        h = self.hasher.copy()
        h.update(self.data[:end-self.offset])
        return h.hexdigest()


_CACHE = {}


def read(path, offset, prefix, *, paginated=False, ordinal_base=0):
    seal = token(path.stat())
    old = _CACHE.get(str(path))
    if old and (old.offset, old.prefix, old.seal) == (offset, prefix, seal):
        return old
    if seal[2] < offset:
        raise ValueError('Consumed Codex history was rewritten; automatic sync stopped')
    h = hashlib.sha256(); ordinal = ordinal_base
    # A matching previously validated digest proves earlier ordinal checks.
    validate_ordinals = paginated and not (old and old.offset == offset and old.prefix == prefix)
    with path.open('rb') as f:
        if token(os.fstat(f.fileno())) != seal:
            raise ValueError('Codex source changed before reading; retry')
        remaining = offset
        if validate_ordinals:
            while remaining:
                line = f.readline(remaining)
                if not line or not line.endswith(b'\n'):
                    raise ValueError('Invalid consumed Codex record boundary')
                if json.loads(line).get('ordinal') != ordinal:
                    raise ValueError('Unsupported or inconsistent paginated rollout ordinals')
                h.update(line); remaining -= len(line); ordinal += 1
        else:
            while remaining:
                block = f.read(min(1024*1024, remaining))
                if not block:raise ValueError('Codex source truncated while reading')
                h.update(block); remaining -= len(block); ordinal += block.count(b'\n')
        if h.hexdigest() != prefix:
            raise ValueError('Consumed Codex history was rewritten; automatic sync stopped')
        data = f.read(seal[2]-offset)
        if token(os.fstat(f.fileno())) != seal or token(path.stat()) != seal:
            raise ValueError('Codex source changed while reading; retry')
    result = Tail(offset, prefix, seal, h, ordinal, data)
    # Bound process-local state and large unconsumed tails.
    if len(_CACHE) >= 8:_CACHE.pop(next(iter(_CACHE)))
    _CACHE[str(path)] = result
    return result


def ready(tail, active, marker, *, paginated=False):
    rows = []; position = tail.offset
    for ordinal, raw in enumerate(tail.data.splitlines(keepends=True), tail.ordinal):
        if not raw.endswith(b'\n'):break
        try:row = json.loads(raw)
        except (ValueError, UnicodeError) as e:
            raise ValueError(f'Malformed Codex record at byte {position}') from e
        if paginated and row.get('ordinal') != ordinal:
            raise ValueError('Unsupported or inconsistent paginated rollout ordinals')
        end = position + len(raw); rows.append((position, end, row)); position = end
        if row.get(marker) or (row.get('type')=='response_item' and str((row.get('payload') or {}).get('id','')).startswith('context-hub-sync:')):continue
        kind = row.get('type'); payload = row.get('payload') or {}
        if kind == 'response_item':active = True
        if kind == 'event_msg' and payload.get('type') in ('task_started', 'turn_started'):active = True
        if (kind == 'event_msg' and payload.get('type') in ('task_complete','turn_complete','turn_aborted')) or (
            kind == 'response_item' and payload.get('phase') in ('final','final_answer')):
            active = False
    return rows, active


def advance(path, tail, end, rows):
    """Retain verified state after the delivery journal commits."""
    h = tail.hasher.copy(); h.update(tail.data[:end-tail.offset])
    _CACHE[str(path)] = Tail(end, h.hexdigest(), tail.seal, h,
                            tail.ordinal+len(rows), tail.data[end-tail.offset:])


def activity(path, end, marker):
    """Recover the writer boundary once for old journals/enrollment."""
    active = False
    with path.open('rb') as source:
        while source.tell() < end:
            raw = source.readline(end-source.tell())
            if not raw.endswith(b'\n'):raise ValueError('Invalid Codex enrollment boundary')
            # Most native rows are transport/usage metadata. Avoid decoding them.
            if b'"response_item"' not in raw and b'"event_msg"' not in raw:continue
            tail=Tail(0,'',(),None,0,raw)
            _,active=ready(tail,active,marker)
    return active
