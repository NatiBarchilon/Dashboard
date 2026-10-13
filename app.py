"""Free, on-demand recording dashboard. No scheduled jobs or messages."""
import json
import logging
import math
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config
from flask import Flask, jsonify, redirect, render_template, request, session, url_for, abort
from recordings import Index, cadence, coverage, radar_rows, waveform

logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'))
log=logging.getLogger('sensor-watch')
app=Flask(__name__)
app.secret_key=os.getenv('SESSION_SECRET') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.getenv('RENDER','false')=='true',MAX_CONTENT_LENGTH=16384)
LOCK=threading.Lock()
STATE={'checked_at':None,'systems':[],'error':None,'scanning':False,'started_at':None}
INDEX=None
INDEX_LOCK=threading.Lock()
HISTORY={}
WAVE_CACHE={}
WAVE_LOCK=threading.Lock()


def sensor_config():
    raw=os.getenv('SENSOR_CONFIG_JSON','')
    if not raw: raise ValueError('SENSOR_CONFIG_JSON is not configured')
    rows=json.loads(raw)
    if not isinstance(rows,list) or not rows: raise ValueError('Invalid sensor configuration')
    return rows


def s3_client():
    return boto3.client('s3',aws_access_key_id=os.environ['S3_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['S3_SECRET_ACCESS_KEY'],region_name=os.getenv('S3_REGION','eu-de'),
        endpoint_url=os.getenv('S3_ENDPOINT_URL') or None,
        config=Config(signature_version=os.getenv('S3_SIGNATURE_VERSION','s3v4'),
                      connect_timeout=5,read_timeout=20,retries={'max_attempts':2},max_pool_connections=12))


def get_index():
    global INDEX
    with INDEX_LOCK:
        if INDEX is None:
            INDEX=Index(s3_client(),os.environ['S3_BUCKET'],os.getenv('DATA_TIMEZONE','Asia/Jerusalem'))
        return INDEX


def iso(t):
    return datetime.fromtimestamp(t,timezone.utc).isoformat()


def system_view(sensor,items,start,end,now):
    seconds,source=cadence(items,sensor.get('segment_seconds'))
    if seconds is None:
        # The user confirmed segmentation at ten minutes or one hour.
        seconds,source=3600,'provisional upper bound; not yet measured'
    cov=coverage(items,start,end,now,seconds,grace=int(os.getenv('UPLOAD_GRACE_SECONDS','120')))
    recorded=sum(s['end']-s['start'] for s in cov['segments'] if s['status']=='recorded')
    pending=sum(s['end']-s['start'] for s in cov['segments'] if s['status']=='pending')
    unknown=sum(s['end']-s['start'] for s in cov['segments'] if s['status']=='unknown')
    due=max(0,end-start-pending)
    return {**sensor,**cov,'files':items,'cadence_seconds':seconds,'cadence_source':source,
            'status':'unknown' if cov['unknown'] else ('missing' if cov['missing_seconds'] else 'ok'),
            'file_count':len(items),'recorded_seconds':recorded,'pending_seconds':pending,
            'unknown_seconds':unknown,'coverage_percent':round(100*recorded/due,1) if due and not unknown else None,
            'gap_count':sum(s['status']=='missing' for s in cov['segments'])}


def loading_system(sensor,start,end):
    return {**sensor,'status':'scanning','files':[],'segments':[],'coverage_percent':None,
            'file_count':0,'missing_seconds':None,'scan_complete':False}


def scan_sensor(index,sensor,start,end,now):
    try:
        items=index.scan(sensor,start,end)
        row=system_view(sensor,items,start,end,now)
        row['scan_complete']=True
        log.info('COVERAGE %s files=%s cadence=%s missing_minutes=%.1f unreadable=%s',sensor['id'],len(items),row['cadence_seconds'],row['missing_seconds']/60,sum(bool(x.get('error')) for x in items))
        if items:
            log.info('RECORDING_FORMAT %s %s',sensor['id'],json.dumps([
                {'name':x.get('name'),'duration':x.get('duration'),'error':x.get('error')} for x in items[:2]]))
        return row
    except Exception:
        log.exception('Sensor scan failed for %s',sensor['id'])
        return {**sensor,'status':'unknown','error':'Could not read storage for this system. Refresh to retry.',
                'files':[],'segments':[{'start':start,'end':end,'status':'unknown'}],
                'coverage_percent':None,'unknown_seconds':end-start,'scan_complete':True}


def scan_into(target,start,end):
    """All systems visible immediately; each scan completes independently."""
    now=time.time()
    error=None
    try:
        index=get_index();sensors=sensor_config()
        with LOCK: target['systems']=[loading_system(s,start,end) for s in sensors]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures={pool.submit(scan_sensor,index,sensor,start,end,now):sensor['id'] for sensor in sensors}
            for future in as_completed(futures):
                row=future.result()
                with LOCK:
                    target['systems']=[row if x['id']==row['id'] else x for x in target['systems']]
                    target['completed_systems']=sum(x.get('scan_complete',False) for x in target['systems'])
        with LOCK: results=list(target['systems'])
        error='Some systems could not be read; their timeline is shown in grey.' if any(x.get('error') for x in results) else None
    except Exception:
        log.exception('Storage scan failed')
        error='Could not scan storage. Refresh to retry.'
    finally:
        with LOCK:
            target.update(checked_at=iso(time.time()),error=error,start=start,end=end,
                          timezone=os.getenv('DATA_TIMEZONE','Asia/Jerusalem'),
                          scanning=False,mode='on_demand',version='2.2')


def start_scan(target,start,end):
    with LOCK:
        if target.get('scanning'): return False
        target.update(scanning=True,started_at=iso(time.time()),start=start,end=end,completed_systems=0,error=None,
                      systems=[loading_system(s,start,end) for s in sensor_config()])
    threading.Thread(target=scan_into,args=(target,start,end),daemon=True,name='requested-storage-scan').start()
    return True


def ensure_recent_scan(force=False):
    now=time.time()
    with LOCK:
        last=STATE.get('checked_at')
        stale=not last or now-datetime.fromisoformat(last).timestamp()>300
    if force or stale: start_scan(STATE,now-86400,now)


@app.before_request
def require_dashboard_login():
    if request.path in ('/health','/login'): return None
    if session.get('authenticated'): return None
    if request.path.startswith('/api/'): return jsonify(error='Authentication required'),401
    return redirect(url_for('login'))


@app.after_request
def security_headers(response):
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    return response


@app.route('/login',methods=['GET','POST'])
def login():
    error=None
    if request.method=='POST':
        username=os.getenv('DASHBOARD_USERNAME','admin');password=os.getenv('DASHBOARD_PASSWORD','')
        if password and secrets.compare_digest(request.form.get('username',''),username) and secrets.compare_digest(request.form.get('password',''),password):
            session.clear();session['authenticated']=True;return redirect(url_for('dashboard'))
        error='Incorrect username or password'
    return render_template('login.html',error=error)


@app.get('/logout')
def logout():
    session.clear();return redirect(url_for('login'))


@app.get('/')
def dashboard():
    ensure_recent_scan(force=True)
    return render_template('index.html')


@app.get('/health')
def health(): return jsonify(ok=True,version='2.2',mode='on_demand')


@app.get('/api/status')
def status():
    ensure_recent_scan(force=request.args.get('refresh')=='1')
    with LOCK: snapshot=dict(STATE)
    return jsonify(snapshot)


@app.post('/api/refresh')
def refresh():
    if not request.is_json: abort(415)
    ensure_recent_scan(force=True)
    return jsonify(queued=True),202


@app.get('/api/history')
def history():
    try:
        start=float(request.args['start']);end=float(request.args['end'])
        if not math.isfinite(start) or not math.isfinite(end) or not 0<end-start<=86400*31 or end>time.time()+60:
            raise ValueError()
    except (KeyError,ValueError): return jsonify(error='Choose a past range of up to 31 days'),400
    key=(start,end)
    with LOCK:
        if key not in HISTORY:
            if len(HISTORY)>8:
                finished=next((k for k,v in HISTORY.items() if not v.get('scanning')),None)
                if finished is not None: del HISTORY[finished]
                else: return jsonify(error='A scan is already running. Please wait.'),429
            HISTORY[key]={'checked_at':None,'systems':[],'error':None,'scanning':False,'started_at':None}
        target=HISTORY[key]
        last=target.get('checked_at')
    if request.args.get('refresh')=='1' or not last or time.time()-datetime.fromisoformat(last).timestamp()>300:
        start_scan(target,start,end)
    with LOCK: snapshot=dict(target)
    return jsonify(snapshot)


def selected_file():
    sensor=next((s for s in sensor_config() if s['id']==request.args.get('sensor')),None)
    key=request.args.get('key','')
    if not sensor or not key.startswith(sensor['prefix'].rstrip('/')+'/') or '.part' in key or not key.endswith(('.mp4','.wav','.csv')):
        abort(404)
    index=get_index();head=index.client.head_object(Bucket=index.bucket,Key=key)
    return index,index.describe({'Key':key,'Size':head['ContentLength'],'ETag':head.get('ETag')})


@app.get('/api/media')
def media():
    index,item=selected_file()
    if item['kind'] not in ('wav','mp4'): abort(400)
    url=index.client.generate_presigned_url('get_object',Params={'Bucket':index.bucket,'Key':item['key'],
        'ResponseContentType':'audio/wav' if item['kind']=='wav' else 'video/mp4','ResponseContentDisposition':'inline'},ExpiresIn=14400)
    return jsonify(url=url,duration=item['duration'],expires_in=14400)


@app.get('/api/waveform')
def audio_wave():
    index,item=selected_file()
    if item['kind']!='wav' or not item.get('wav'): return jsonify(error=item.get('error','Unsupported WAV')),422
    cachekey=(item['key'],item['size'],item['duration'])
    with WAVE_LOCK:
        if cachekey not in WAVE_CACHE:
            if len(WAVE_CACHE)>12: WAVE_CACHE.clear()
            WAVE_CACHE[cachekey]=waveform(index.client,index.bucket,item['key'],item['size'],item['wav'])
        result=WAVE_CACHE[cachekey]
    return jsonify(result)


@app.get('/api/radar')
def radar():
    index,item=selected_file()
    if item['kind']!='csv': abort(400)
    rows=radar_rows(index.client,index.bucket,item['key'],item['size'],index.tz)
    if not rows: return jsonify(error='No readable timestamps in this radar CSV; its column format has been logged for diagnosis.'),422
    bins={}
    for row in rows:
        minute=int(row['t']//60)*60;bins.setdefault(minute,[]).append(row)
    points=[]
    for t,values in sorted(bins.items()):
        known=[x['presence'] for x in values if x['presence'] is not None]
        points.append({'t':t,'presence_percent':100*sum(known)/len(known) if known else None,'samples':len(values),
                       'max_moving_energy':max((x['moving_energy'] for x in values if x['moving_energy'] is not None),default=None)})
    return jsonify(points=points,rows=len(rows),unknown_states=sum(x['presence'] is None for x in rows))


@app.errorhandler(Exception)
def api_error(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc,HTTPException): return exc
    log.exception('Request failed')
    return jsonify(error='Unable to read this data. Please refresh and try again.'),503


if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
