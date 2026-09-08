# FreezeM Sensor Watch

Web dashboard for monitoring the newest files from four sensor prefixes in an S3 or S3-compatible bucket. Each root contains changing `YYYY/MM/DD` folders; the app checks today and yesterday automatically. A system becomes stale when its newest object is older than two hours.

## Local run

1. Copy `.env.example` to `.env` and fill the values.
2. Install: `pip install -r requirements.txt`
3. Export the variables and run: `python app.py`

## Configuration

- `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`: required.
- `S3_ENDPOINT_URL`: leave blank for AWS S3; set it for compatible storage.
- `SENSOR_CONFIG_JSON`: JSON array with `id`, Hebrew/English `name`, and S3 `prefix` for each system.
- `ALERT_WEBHOOK_URL`: optional Slack/Teams-compatible webhook.
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`: optional Telegram alerts.
- `STALE_AFTER_HOURS`: defaults to 2.
- `CHECK_INTERVAL_MINUTES`: defaults to 10.
- `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD`: HTTP Basic Auth credentials for the dashboard.
- Email alerts: set `EMAIL_TO`, `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, and `SMTP_PASSWORD`.

The app only lists object metadata and never downloads sensor files. Grant its access key read/list access to the configured bucket and prefixes only.

## Render

`render.yaml` defines a Frankfurt web service. Add the repository to GitHub/GitLab/Bitbucket, then create a Render Blueprint and enter the secret environment variables when prompted.

For uninterrupted monitoring, upgrade the web service from Free to Starter; Free services may sleep while idle.
