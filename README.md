# FreezeM Recording Observatory v2.1

On-demand dashboard for two cameras, radar, and microphone. Runs on the existing Free Render service. No emails, notifications, schedules, database, persistent disk, or new paid service.

## User flow

1. Open https://freezem-sensor-watch.onrender.com and sign in using the existing account.
2. Opening the dashboard queues a storage scan. The HTTP response returns immediately; the page polls progress as each system is scanned.
3. Each system gets a green/red/yellow/grey timeline and a collection percentage. Red means missing, yellow is a current file that may still be recording/uploading, and grey means data could not be verified.
4. Click a red interval to see system, start, end, and duration. The table lists all gaps. Each card shows total missing time and the longest gap.
5. Choose the last 3/24/72 hours or a past date. History is scanned asynchronously to avoid the existing web service's HTTP timeout.
6. Refresh storage to see whether late uploads have filled the original gaps. No messages are ever sent.
7. Select a recording or click a green interval to view radar data, listen to audio and see its waveform, or play/seek camera video.

The service can sleep when nobody is viewing it. No work runs at import, at startup, or periodically without a request. Page visits and status requests can refresh a cache older than five minutes while the dashboard is open. Nothing needs to run continuously.

## Recording integrity

- Recording start is parsed from the filename in Asia/Jerusalem time, never from S3 LastModified.
- WAV duration uses the RIFF data header. MP4 duration uses movie metadata through small range reads, without downloading the video.
- Radar CSV timestamps identify sample continuity; gaps longer than five seconds split coverage.
- Cadence is measured from at least three valid file durations. Until measured, the user's stated one-hour maximum is used to avoid declaring an unfinished recording missing.
- A 120-second upload grace is configurable with UPLOAD_GRACE_SECONDS.
- Storage failures remain unverified and do not turn the whole system red. An individual sensor failure does not prevent the other systems from appearing.
- Repeated-hour timestamps at the DST change are unverified because a timezone-less filename cannot identify which hour it belongs to.
- Audio waveform is digital RMS in dBFS, not calibrated sound pressure. PCM WAV files up to 512 MB are supported for waveform generation. Audio and video codec support also depends on the browser.
- Media access uses four-hour presigned URLs. Storage credentials stay server-side; login is preserved.

## Existing service

- Repository: https://github.com/NatiBarchilon/Dashboard
- Render service: srv-dag2sg0hchos73fkt41g
- Confirmed workspace: tea-dag2aitbedkc738eu1q0 (Nati's workspace)
- Keep the existing Free plan, storage configuration, and login secrets.
- No email credentials are needed. Existing email/alert variables are ignored by this version.

Production requires the existing S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET, S3_ENDPOINT_URL, S3_REGION, SENSOR_CONFIG_JSON, DASHBOARD_PASSWORD and SESSION_SECRET. Keep these in Render Environment, never in Git.

The current service start command (gunicorn --workers 1 --threads 4 --timeout 60 app:app) works because scans run asynchronously. render.yaml allows a longer timeout for on-demand waveform requests if the service is later managed by Blueprint; it keeps `plan: free`.

## Verification

`python -m pip install -r requirements.txt pytest`

`python -m pytest -q`

Tests cover filename timestamps, daylight saving ambiguity, WAV/MP4 metadata, wave amplitude, radar sample gaps, delayed backfills, unfinished files, collection percentages, access control, per-system errors, scan progress, and ensuring requests are required to trigger scans.

Production storage formats and media playback must also be checked after deployment with the existing server credentials.
