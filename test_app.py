import io
import os
import struct
import wave
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest

os.environ['DISABLE_BACKGROUND_MONITOR']='true'
import app
from recordings import recording_time,coverage,cadence,wav_info,mp4_duration,waveform,radar_rows,Index

@pytest.fixture(autouse=True)
def environment(monkeypatch,tmp_path):
    monkeypatch.setattr(app,'STATE',{'checked_at':None,'systems':[],'error':None,'scanning':False,'started_at':None})
    monkeypatch.setenv('SENSOR_CONFIG_JSON','[{"id":"mic","name":"Microphone","prefix":"sensor/audio/Mic1/"}]')

@pytest.mark.parametrize('name',[
 'P430_2026-08-30_13-29-41.mp4','2026_08_30_132941.csv','30_08_2026 132941.wav'])
def test_filename_uses_recording_time(name):
    assert recording_time(name,'Asia/Jerusalem')==datetime(2026,8,30,10,29,41,tzinfo=timezone.utc).timestamp()

def test_dst_ambiguity_not_guessed():
    assert recording_time('P430_2026-10-25_01-30-00.mp4','Asia/Jerusalem') is None

def file(a,b):return {'start':a,'end':b,'duration':b-a}

def test_internal_gap_and_late_backfill():
    items=[file(0,3600),file(7800,10800)]
    assert coverage(items,0,10800,10800,3600)['missing_seconds']==4200
    items.append(file(3600,7800))
    assert coverage(items,0,10800,10800,3600)['missing_seconds']==0

def test_open_hourly_file_not_missing():
    c=coverage([file(0,3600)],0,7100,7100,3600)
    assert c['missing_seconds']==0
    assert c['segments'][-1]['status']=='pending'

def test_overdue_hourly_file_missing():
    c=coverage([file(0,3600)],0,7380,7380,3600)
    assert c['missing_seconds']==3780

def test_unknown_metadata_is_not_false_missing():
    c=coverage([{'start':0,'error':'bad header'}],0,10800,10800,3600)
    assert c['unknown'] and c['missing_seconds']==0

def test_cadence_uses_durations_not_gaps():
    items=[file(0,600),file(7200,7800),file(14400,15000)]
    assert cadence(items)==(600,'measured from files')

def test_csv_internal_sample_gap():
    item={**file(0,10000),'spans':[[0,2000],[6500,10000]]}
    assert coverage([item],0,10000,10000,3600)['missing_seconds']==4500

class Storage:
    def __init__(self,content):self.content=content
    def get_object(self,**args):
        b=self.content
        if 'Range' in args:
            a,z=map(int,args['Range'][6:].split('-'));b=b[a:z+1]
        return {'Body':io.BytesIO(b)}

def test_wav_duration_and_waveform():
    b=io.BytesIO()
    with wave.open(b,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(struct.pack('<h',16384)*16000)
    client=Storage(b.getvalue());meta=wav_info(client,'b','k',len(client.content))
    assert meta['duration']==2
    points=waveform(client,'b','k',len(client.content),meta)['points']
    assert abs(points[0]['rms_dbfs']+6.0206)<.001

def test_mp4_reads_duration_without_video_download():
    def atom(kind,payload):return struct.pack('>I4s',len(payload)+8,kind)+payload
    mvhd=b'\0'*12+struct.pack('>II',1000,600000)+b'\0'*12
    content=atom(b'ftyp',b'isom')+atom(b'mdat',b'x'*1000)+atom(b'moov',atom(b'mvhd',mvhd))
    assert mp4_duration(Storage(content),'b','k',len(content))==600

def test_radar_presence_and_timestamps():
    content=b'timestamp_local,timestamp_utc,state_code,detection_distance_cm\n2026-08-30 13:00:00,2026-08-30T10:00:00Z,1,100\n2026-08-30 13:00:01,2026-08-30T10:00:01Z,0,0\n'
    rows=radar_rows(Storage(content),'b','k',len(content),'Asia/Jerusalem')
    assert [r['presence'] for r in rows]==[1,0]
    assert rows[1]['t']-rows[0]['t']==1

def test_health_and_private_api():
    client=app.app.test_client()
    assert client.get('/health').json['mode']=='on_demand'
    for url in ['/api/status','/api/history','/api/media','/api/waveform','/api/radar']:
        assert client.get(url).status_code==401

def test_outside_prefix_rejected():
    client=app.app.test_client()
    with client.session_transaction() as session:session['authenticated']=True
    assert client.get('/api/media?sensor=mic&key=private/secret.wav').status_code==404

def test_one_sensor_failure_does_not_hide_others(monkeypatch):
    monkeypatch.setattr(app,'sensor_config',lambda:[{'id':'bad','name':'bad','prefix':'bad/'},{'id':'ok','name':'ok','prefix':'ok/'}])
    idx=Mock();idx.scan.side_effect=[RuntimeError(),[file(app.time.time()-86400,app.time.time())]]
    monkeypatch.setattr(app,'get_index',lambda:idx)
    app.scan_into(app.STATE,app.time.time()-86400,app.time.time())
    result=app.STATE
    assert len(result['systems'])==2
    assert result['systems'][0]['status']=='unknown'
    assert result['systems'][1]['status']=='ok'


def test_scan_only_starts_on_authenticated_visit(monkeypatch):
    start=Mock();monkeypatch.setattr(app,'start_scan',start)
    client=app.app.test_client()
    client.get('/health')
    client.get('/')
    client.get('/api/status')
    assert start.call_count==0
    with client.session_transaction() as session:session['authenticated']=True
    client.get('/')
    assert start.call_count==1


def test_status_does_not_duplicate_in_progress_scan(monkeypatch):
    threads=Mock();monkeypatch.setattr(app.threading,'Thread',threads)
    app.ensure_recent_scan()
    app.ensure_recent_scan()
    assert threads.call_count==1
    assert app.STATE['scanning']


def test_late_upload_updates_missing_after_refresh(monkeypatch):
    now=app.time.time();start=now-86400
    idx=Mock();idx.scan.return_value=[file(start,start+3600),file(start+10800,now)]
    monkeypatch.setattr(app,'get_index',lambda:idx)
    app.scan_into(app.STATE,start,now)
    assert app.STATE['systems'][0]['missing_seconds']==7200
    idx.scan.return_value=[file(start,now)]
    app.scan_into(app.STATE,start,now)
    assert app.STATE['systems'][0]['missing_seconds']==0
    assert 'email_configured' not in app.STATE


def test_percentage_excludes_current_unfinished_file():
    row=app.system_view({'id':'mic'},[file(0,3600)],0,5000,5000)
    assert row['coverage_percent']==100 and row['pending_seconds']==1400


def test_history_is_async_not_a_blocking_http_scan(monkeypatch):
    monkeypatch.setattr(app,'HISTORY',{})
    start=Mock();monkeypatch.setattr(app,'start_scan',start)
    client=app.app.test_client()
    with client.session_transaction() as session:session['authenticated']=True
    response=client.get('/api/history?start=100&end=10000')
    assert response.status_code==200 and start.call_count==1
    assert client.get('/api/history?start=nan&end=10000').status_code==400
