"""Recording time index. Upload time never determines recording coverage."""
import csv
import io
import math
import os
import re
import struct
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
PATTERNS = [
    (r'(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})', '%Y-%m-%d_%H-%M-%S'),
    (r'(\d{4}_\d{2}_\d{2}_\d{6})', '%Y_%m_%d_%H%M%S'),
    (r'(\d{2}_\d{2}_\d{4}[ _]\d{6})', '%d_%m_%Y %H%M%S'),
    (r'(\d{8}_\d{6})', '%Y%m%d_%H%M%S'),
]


def recording_time(key, tz):
    name = key.rsplit('/', 1)[-1]
    for pattern, fmt in PATTERNS:
        match = re.search(pattern, name)
        if match:
            value = match.group(1)
            if fmt.startswith('%d_'):
                value = value[:10] + ' ' + value[11:]
            try:
                local = datetime.strptime(value, fmt).replace(tzinfo=ZoneInfo(tz))
                # Naive filenames in the repeated DST hour are ambiguous; do not guess.
                if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
                    return None
                return local.astimezone(UTC).timestamp()
            except ValueError:
                return None
    return None


def read_range(client, bucket, key, offset, length):
    body = client.get_object(Bucket=bucket, Key=key, Range=f'bytes={offset}-{offset+length-1}')["Body"]
    try:
        return body.read()
    finally:
        body.close()


def wav_info(client, bucket, key, size):
    head = read_range(client, bucket, key, 0, min(size, 131072))
    if head[:4] != b'RIFF' or head[8:12] != b'WAVE':
        raise ValueError('Unsupported WAV container')
    pos = 12
    fmt = None
    while pos + 8 <= len(head):
        kind, length = struct.unpack_from('<4sI', head, pos)
        if kind == b'fmt ':
            fmt = struct.unpack_from('<HHIIHH', head, pos + 8)
        if kind == b'data' and fmt:
            code, channels, rate, byte_rate, align, bits = fmt
            if not byte_rate or not align:
                raise ValueError('Invalid WAV header')
            actual = min(length, max(0, size - pos - 8))
            return dict(duration=actual / byte_rate, offset=pos+8, data_bytes=actual,
                        code=code, channels=channels, rate=rate, align=align, bits=bits)
        pos += 8 + length + length % 2
    raise ValueError('WAV data header not found')


def mp4_duration(client, bucket, key, size):
    # Skip mdat via atom sizes; never download the video just to determine duration.
    pos = 0
    for _ in range(100):
        if pos + 8 > size:
            break
        head = read_range(client, bucket, key, pos, min(16, size-pos))
        length, kind = struct.unpack_from('>I4s', head)
        header = 8
        if length == 1:
            length = struct.unpack_from('>Q', head, 8)[0]
            header = 16
        if length == 0:
            length = size - pos
        if length < header or pos + length > size:
            break
        if kind == b'moov':
            child = pos + header
            for _ in range(100):
                if child + 8 > pos + length:
                    break
                data = read_range(client, bucket, key, child, min(48, pos+length-child))
                n, typ = struct.unpack_from('>I4s', data)
                if typ == b'mvhd':
                    version = data[8]
                    scale = struct.unpack_from('>I', data, 28 if version == 1 else 20)[0]
                    ticks = struct.unpack_from('>Q' if version == 1 else '>I', data, 32 if version == 1 else 24)[0]
                    if scale and ticks:
                        return ticks / scale
                if n < 8:
                    break
                child += n
            break
        pos += length
    raise ValueError('MP4 duration not found')


def parse_csv_time(row, tz):
    value = row.get('timestamp_utc') or row.get('timestamp_local') or row.get('timestamp') or row.get('time')
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC if row.get('timestamp_utc') else ZoneInfo(tz))
        return dt.timestamp()
    except ValueError:
        return None


def radar_rows(client, bucket, key, size, tz):
    if size > 32 * 1024 * 1024:
        raise ValueError('CSV exceeds 32 MB limit')
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        content = body.read().decode('utf-8-sig')
    finally:
        body.close()
    rows = []
    for row in csv.DictReader(io.StringIO(content)):
        t = parse_csv_time(row, tz)
        if t is None:
            continue
        state = row.get('state_code', row.get('presence', row.get('state', '')))
        try:
            presence = 1 if int(state) in (1, 2, 3) else (0 if int(state) == 0 else None)
        except (ValueError, TypeError):
            presence = {'true':1, 'false':0, 'moving':1, 'stationary':1, 'both':1,
                        'no target':0, 'none':0}.get(str(state).lower())
        def number(name):
            try:
                v = float(row[name])
                return v if math.isfinite(v) else None
            except (KeyError, ValueError, TypeError):
                return None
        rows.append({'t': t, 'presence': presence, 'distance_cm': number('detection_distance_cm'),
                     'moving_energy': number('moving_energy'), 'stationary_energy': number('stationary_energy')})
    return sorted(rows, key=lambda r:r['t'])


class Index:
    def __init__(self, client, bucket, tz='Asia/Jerusalem'):
        self.client, self.bucket, self.tz = client, bucket, tz
        self.cache = {}

    def objects(self, prefix, start, end):
        day = datetime.fromtimestamp(start, ZoneInfo(self.tz)).date() - timedelta(days=1)
        last = datetime.fromtimestamp(end, ZoneInfo(self.tz)).date()
        result = []
        while day <= last:
            path = f"{prefix.rstrip('/')}/{day:%Y/%m/%d}/"
            for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=path):
                result.extend(o for o in page.get('Contents', []) if o.get('Size', 0) and '.part' not in o['Key'] and o['Key'].lower().endswith(('.wav','.csv','.mp4')))
            day += timedelta(days=1)
        return result

    def describe(self, obj):
        key = obj['Key']
        cachekey = (key, obj.get('ETag'), obj['Size'])
        if cachekey in self.cache:
            return self.cache[cachekey]
        start = recording_time(key, self.tz)
        item = {'key': key, 'name': key.rsplit('/',1)[-1], 'size': obj['Size'], 'start': start,
                'end': None, 'duration': None, 'kind': key.rsplit('.',1)[-1].lower()}
        try:
            if start is None:
                raise ValueError('Unknown or ambiguous filename timestamp')
            if item['kind'] == 'wav':
                meta = wav_info(self.client,self.bucket,key,obj['Size'])
                duration = meta['duration']
                item['wav'] = meta
            elif item['kind'] == 'mp4':
                duration = mp4_duration(self.client,self.bucket,key,obj['Size'])
            else:
                rows = radar_rows(self.client,self.bucket,key,obj['Size'],self.tz)
                if not rows:
                    raise ValueError('CSV has no readable timestamps')
                # A CSV container is not evidence that all sensor samples exist.
                spans = []
                a = b = rows[0]['t']
                for row in rows[1:]:
                    if row['t'] - b > 5:
                        spans.append([a,b+1]); a = row['t']
                    b = row['t']
                spans.append([a,b+1])
                item['spans'] = spans
                duration = max(0,rows[-1]['t']+1-start)
            if not 0 < duration < 86400*2:
                raise ValueError('Invalid recording duration')
            item.update(duration=duration, end=start+duration)
        except Exception as exc:
            item['error'] = str(exc) if isinstance(exc,ValueError) else 'Storage read failed'
        if not item.get('error'):
            if len(self.cache) > 10000:
                self.cache.clear()
            self.cache[cachekey] = item
        return item

    def scan(self,sensor,start,end):
        objects = self.objects(sensor['prefix'],start,end)
        with ThreadPoolExecutor(max_workers=4) as pool:
            items = list(pool.map(self.describe,objects))
        return sorted([x for x in items if x['start'] is None or x.get('end') is None or (x['end'] > start and x['start'] < end)],key=lambda x:x['start'] or 0)


def cadence(items, override=None):
    if override:
        return float(override), 'configured'
    durations = [x['duration'] for x in items if x.get('duration')]
    if len(durations) < 3:
        return None, 'insufficient data'
    buckets = Counter(round(x/60)*60 for x in durations if x>=30)
    if not buckets:
        return None, 'insufficient data'
    value, count = buckets.most_common(1)[0]
    if count < 3:
        return None, 'irregular durations'
    return value, 'measured from files'


def coverage(items,start,end,now,cadence_seconds,grace=120,tolerance=3):
    spans = []
    unknown = any(x.get('error') for x in items)
    for item in items:
        if item.get('end'):
            for a,b in item.get('spans',[[item['start'],item['end']]]):
                a,b=max(start,a),min(end,b)
                if b>a:
                    spans.append([a,b])
    merged=[]
    for a,b in sorted(spans):
        if merged and a <= merged[-1][1]+tolerance:
            merged[-1][1]=max(b,merged[-1][1])
        else:
            merged.append([a,b])
    segments=[]; cursor=start
    def gap(a,b,trailing=False):
        if b-a<=tolerance:
            return
        status='missing'
        if unknown:
            status='unknown'
        elif trailing and b>=now-grace-1:
            if cadence_seconds is None:
                status='unknown'
            elif now-a <= cadence_seconds+grace:
                status='pending'
        segments.append({'start':a,'end':b,'status':status})
    for a,b in merged:
        gap(cursor,a)
        segments.append({'start':a,'end':b,'status':'recorded'})
        cursor=b
    gap(cursor,end,True)
    missing=[x for x in segments if x['status']=='missing']
    return {'segments':segments,'missing_seconds':sum(x['end']-x['start'] for x in missing),
            'longest_gap_seconds':max((x['end']-x['start'] for x in missing),default=0),
            'unknown':unknown or any(x['status']=='unknown' for x in segments)}


def waveform(client,bucket,key,size,meta):
    import numpy as np
    if meta['code'] != 1 or meta['bits'] not in (8,16,24,32):
        raise ValueError('Waveform supports PCM WAV 8/16/24/32-bit; playback may still work')
    if meta['data_bytes'] > 512*1024*1024:
        raise ValueError('Waveform file exceeds 512 MB; use audio playback')
    frames = meta['data_bytes']//meta['align']
    blockframes=max(1, math.ceil(frames/1000))
    blockbytes=blockframes*meta['align']
    body=client.get_object(Bucket=bucket,Key=key,Range=f"bytes={meta['offset']}-{meta['offset']+meta['data_bytes']-1}")["Body"]
    points=[]; offset=0
    try:
        while offset < frames:
            data=body.read(min(blockbytes,(frames-offset)*meta['align']))
            if not data: break
            if meta['bits']==24:
                raw=np.frombuffer(data,dtype=np.uint8).reshape(-1,3).astype(np.int32)
                samples=raw[:,0] | (raw[:,1]<<8) | (raw[:,2]<<16)
                samples=((samples ^ 0x800000)-0x800000).astype(float)/8388608
            elif meta['bits']==8:
                samples=(np.frombuffer(data,dtype=np.uint8).astype(float)-128)/128
            else:
                samples=np.frombuffer(data,dtype='<i'+str(meta['bits']//8)).astype(float)/(2**(meta['bits']-1))
            rms=float(np.sqrt(np.mean(samples*samples)))
            points.append({'seconds':offset/meta['rate'],'rms_dbfs':max(-100,20*math.log10(max(rms,1e-5))), 'peak':float(np.max(np.abs(samples)))})
            offset+=len(data)//meta['align']
    finally: body.close()
    return {'points':points,'duration':meta['duration'],'units':'dBFS (digital level, not calibrated sound pressure)'}
