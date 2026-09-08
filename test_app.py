import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DISABLE_BACKGROUND_MONITOR"] = "true"
os.environ["S3_BUCKET"] = "test"

import app


def test_health():
    response = app.app.test_client().get("/health")
    assert response.status_code == 200
    assert response.json == {"ok": True}


def test_stale_detection():
    old = {"Key": "mic/file.wav", "LastModified": datetime.now(timezone.utc) - timedelta(hours=3)}
    with patch.object(app, "s3_client"), patch.object(app, "sensor_config", return_value=[{"id":"mic","name":"Mic","prefix":"mic/"}]), patch.object(app, "newest_sensor_object", return_value=old):
        result = app.run_check(send_notifications=False)
    assert result["systems"][0]["status"] == "stale"
