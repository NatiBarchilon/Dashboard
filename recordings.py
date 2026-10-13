"""Recording time index. Upload time never determines recording coverage."""
import csv
import io
import json
import math
import logging
import re
import struct
from collections import Counter
from statistics import median
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("sensor-watch")
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
    raise ValueError('MP4 duration is stored in fragments; use filename coverage and browser playback')


def normalized_row(row):
    return {re.sub(r'[^a-z0-9]+','_',str(k).strip().lower()).strip('_'):v for k,v in row.items() if k is not None}


def parse_csv_time(row, tz):
    row = normalized_row(row)
    fields = ('timestamp_utc','ts_utc','utc_iso','timestamp_iso','ts_iso','timestamp_local',
              'ts_local','local_iso','datetime','date_time','timestamp','time_iso','ts','time','datetime_utc',
              'iso_time','utc_time','time_utc','utc','t','timestamp_s','timestamp_ms','ts_ms','unix_time','epoch')
    extra=sorted((k for k,v in row.items() if k not in fields and ('time' in k or k.startswith('ts') or k.endswith('_iso') or re.match(r'^20\d{2}[-_/]\d{2}[-_/]\d{2}[T _]',str(v or '')))),key=lambda k:('utc' not in k,k))
    for field in (*fields,*extra):
        value = str(row.get(field) or '').strip()
        if not value:
            continue
        try:
            number = float(value)
            if number > 1e15: number /= 1e6
            elif number > 1e12: number /= 1e3
            if 1e9 < number < 1e11:
                return number
        except ValueError:
            pass
        value = re.sub(r'\s+(UTC|GMT)$','+00:00',value).replace('Z','+00:00')
        if len(value) > 10 and value[10] == '_': value = value[:10]+' '+value[11:]
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            dt = None
            for fmt in ('%Y_%m_%d %H:%M:%S','%d_%m_%Y %H:%M:%S','%d/%m/%Y %H:%M:%S',
                        '%Y-%m-%d %H:%M:%S.%f','%Y/%m/%d %H:%M:%S','%Y_%m_%d_%H%M%S','%d_%m_%Y %H%M%S'):
                try: dt = datetime.strptime(value,fmt);break
                except ValueError: continue
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC if 'utc' in field else ZoneInfo(tz))
            return dt.timestamp()
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
    try: dialect=csv.Sniffer().sniff(content[:8192],delimiters=',;\t')
    except csv.Error: dialect='excel'
    reader=csv.DictReader(io.StringIO(content),dialect=dialect)
    for row in reader:
        row=normalized_row(row)
        t = parse_csv_time(row, tz)
        if t is None:
            continue
        state = row.get('state_code', row.get('target_state', row.get('target_status', row.get('presence', row.get('presence_state', row.get('state', row.get('status', row.get('target_type',''))))))))
        try:
            presence = 1 if int(float(state)) in (1, 2, 3) else (0 if int(float(state)) == 0 else None)
        except (ValueError, TypeError):
            presence = {'true':1, 'false':0, 'moving':1, 'stationary':1, 'both':1,
                        'moving & stationary':1, 'moving+stationary':1, 'motion':1, 'static':1, 'occupied':1, 'present':1, 'no_target':0, 'absent':0, 'no target':0, 'none':0}.get(str(state).lower())
        def number(name):
            try:
                v = float(row[name])
                return v if math.isfinite(v) else None
            except (KeyError, ValueError, TypeError):
                return None
        rows.append({'t': t, 'presence': presence, 'distance_cm': number('detection_distance_cm'),
                     'moving_energy': number('moving_energy'), 'stationary_energy': number('stationary_energy')})
    if not rows:
        log.warning('RADAR_COLUMNS %s',json.dumps(reader.fieldnames or []))
    return sorted(rows, key=lambda r:r['t'])


class Index:
    def __init__(self, client, bucket, tz='Asia/Jerusalem'):
        self.client, self.bucket, self.tz = client, bucket, tz
        self.cache = {}
        self.radar_columns_logged = set()

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
        # Coverage needs the recording filenames, not thousands of media downloads.
        # Content metadata and plots are read only for a selected recording.
        objects = self.objects(sensor['prefix'],start,end)
        if sensor['id'] not in self.radar_columns_logged:
            sample=next((o for o in objects if o['Key'].lower().endswith('.csv')),None)
            if sample:
                try:
                    text=read_range(self.client,self.bucket,sample['Key'],0,min(4096,sample['Size'])).decode('utf-8-sig')
                    lines=text.splitlines()
                    log.info('RADAR_HEADER %s %s',sensor['id'],json.dumps(lines[:2]))
                    self.radar_columns_logged.add(sensor['id'])
                except Exception:
                    log.warning('Radar header sample could not be read for %s',sensor['id'])
        described=[]
        for obj in objects:
            t=recording_time(obj['Key'],self.tz)
            # The prior-day listing covers files spanning midnight. Discard other
            # out-of-window records before any metadata request or unknown marker.
            if t is not None and (t < start-7200 or t >= end): continue
            kind=obj['Key'].rsplit('.',1)[-1].lower()
            described.append({'key':obj['Key'],'name':obj['Key'].rsplit('/',1)[-1],
                'start':t,'size':obj['Size'],'kind':kind,'etag':obj.get('ETag')})
        times=sorted(set(x['start'] for x in described if x['start'] is not None))
        default=600 if any(x['kind']=='mp4' for x in described) or any(x in sensor['id'].lower() for x in ('cam','cage')) else 3600
        deltas=[b-a for a,b in zip(times,times[1:]) if 30<=b-a<=7200]
        counts=Counter(round(x/60)*60 for x in deltas)
        nominal=float(sensor.get('segment_seconds') or default)
        source='expected segment length'
        if not sensor.get('segment_seconds') and counts:
            candidate,count=counts.most_common(1)[0]
            # Only infer regular supported segmentation, rather than treating a
            # long outage or every-other-file gap as a longer recording.
            if count>=3 and candidate in (300,600,900,3600):
                nominal=median([x for x in deltas if abs(x-candidate)<30])
                source='measured filename intervals'
        if sensor.get('segment_seconds'): source='configured segment length'
        out=[]
        for item in described:
            if item['start'] is None:
                item.update(end=None,duration=None,error='Unknown or ambiguous filename timestamp')
            else:
                item.update(duration=nominal,end=item['start']+nominal,
                            duration_source=source,coverage_basis='filename',nominal_seconds=nominal)
                if item['end']<=start: continue
            out.append(item)
        return sorted(out,key=lambda x:x['start'] or 0)



def cadence(items, override=None):
    if override:
        return float(override), 'configured'
    inferred=next((x for x in items if x.get('nominal_seconds')),None)
    if inferred: return inferred['nominal_seconds'],inferred['duration_source']
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
