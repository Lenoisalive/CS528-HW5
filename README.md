# HW5: Request Logging and Performance Measurement

## 1. Overview

This assignment extends the HW4 Cloud Storage file server with PostgreSQL request logging and separate timing measurements for request parsing, file downloads, database writes, and response output. It also adds repeatable deployment, resource cleanup, and an authenticated hourly Cloud SQL shutdown task while preserving the existing Pub/Sub subscriber.

## 2. File Structure

```text
hw5/
├── README.md
├── requirement.md
├── LICENSE
├── setup.sh
├── startup.sh
├── cleanup.sh
├── scheduler.sh
├── setup_schema.py
├── requirements-db.txt
├── deploy.sh
├── http-client
├── http-client_linux
├── http-client.exe
├── analysis/
│   └── summarize_timings.py
├── cloud_function/
│   ├── .gcloudignore
│   ├── main.py
│   └── requirements.txt
├── deploy/
│   ├── hw5-common.sh
│   ├── hw5_validate.py
│   ├── package_app.py
│   ├── vm_bootstrap.py
│   ├── hw5-schema.service
│   ├── hw5-web.service
│   ├── hw5-subscriber.service
│   ├── validate_resource.py
│   ├── startup.sh
│   ├── forbidden-startup.sh
│   ├── hw4-web.service
│   └── hw4-forbidden.service
├── service1/
│   ├── server.py
│   ├── database.py
│   ├── timing.py
│   └── requirements.txt
├── service2/
│   ├── subscriber.py
│   └── requirements.txt
└── stress/
    ├── probe_receiver.py
    ├── run_clients.py
    ├── monitor.py
    ├── plot_performance.py
    └── requirements-plot.txt
```

## 3. File Descriptions

| File | Purpose |
|---|---|
| `analysis/summarize_timings.py` | Summarizes timing records by experiment, reporting sample counts, means, P50, P95, maxima, and execution outcomes. |
| `cloud_function/main.py` | Handles Scheduler requests and stops the configured Cloud SQL instance by setting its activation policy to `NEVER`. |
| `deploy/hw5-common.sh` | Defines shared deployment settings and helpers for resource checks, ownership validation, retries, SQL operations, and Scheduler state changes. |
| `deploy/hw5_validate.py` | Validates resource ownership metadata before deployment scripts reuse or remove resources. |
| `deploy/package_app.py` | Builds a reproducible source archive and prints its SHA-256 hash without packaging credentials or runtime logs. |
| `deploy/vm_bootstrap.py` | Reads VM metadata, retrieves and verifies the application archive, and configures the application using a password from Secret Manager. |
| `deploy/hw5-schema.service` | Runs schema setup before the web service starts. |
| `deploy/hw5-web.service` | Runs the Flask application through Gunicorn under systemd. |
| `deploy/hw5-subscriber.service` | Runs the unchanged Pub/Sub subscriber under systemd. |
| `stress/probe_receiver.py` | Captures requests locally to inspect the supplied client's headers, paths, and request bodies. |
| `cleanup.sh` | Removes HW5 deployment resources, releases the static IP, stops Cloud SQL without deleting its data, preserves the HW2 bucket, and revokes local ADC when present. |
| `scheduler.sh` | Provides commands to inspect, pause, resume, or manually trigger the hourly SQL shutdown job. |
| `setup_schema.py` | Creates missing database tables without clearing existing data. |
| `setup.sh` | Provisions or reuses cloud resources, configures identities and permissions, and deploys the VM, shutdown function, and Scheduler job. |
| `startup.sh` | Installs and configures the VM application and systemd services, creating an installation marker only after successful setup. |

## 4. Configuration and Usage

### Prerequisites

Run local commands from the `hw5` directory with Python 3 and the Google Cloud CLI installed, using an account authorized to manage the project's APIs, IAM, Compute Engine, Cloud SQL, Storage, Pub/Sub, Secret Manager, functions, and Scheduler resources.

| Setting | Value |
|---|---|
| Project ID | `cs-528-508021` |
| Region / zone | `us-central1` / `us-central1-f` |
| Application VM | `hw5-app`, `e2-small` |
| Web port | `8080` |
| Static IP resource | `hw5-web-ip` |
| Existing file bucket / prefix | `gs://cs528hw2/pages/` |
| Cloud SQL instance | `hw5-postgres`, PostgreSQL 16 |
| SQL database / user | `hw5` / `hw5_app` |
| Database password secret | `hw5-db-password` |
| Pub/Sub topic / subscription | `hw5-forbidden-requests` / `hw5-forbidden-sub` |
| Shutdown function / Scheduler job | `hw5-stop-sql` / `hw5-hourly-stop-sql` |

The project ID is hardcoded in the deployment configuration; the existing HW2 file bucket must be available before setup.

### Deploy the applications

```bash
gcloud auth login
gcloud config set project cs-528-508021
chmod +x setup.sh startup.sh cleanup.sh scheduler.sh

HW5_EXPERIMENT_ID=hw5-load-01 ./setup.sh
```

Setup enables APIs, configures service accounts and permissions, starts or creates Cloud SQL, creates missing database resources, reserves a static IP, and deploys the application through VM startup metadata.

On the VM, `startup.sh` installs dependencies and enables the schema, web, and subscriber systemd services; subsequent boots skip installation once its completion marker exists.

Repeated setup preserves database rows and reuses compatible resources, but rejects an existing VM with a different application archive hash; export its logs before replacing it with an updated deployment.

The VM retrieves its database password from Secret Manager, so credentials should not be committed to source control; local Python access to cloud services additionally requires `gcloud auth application-default login`.

Use `setup.sh` and the root `startup.sh` for HW5; `deploy.sh` and the retained HW4 deployment files belong to the earlier assignment.

### Run requests and load tests

Pause automatic SQL shutdown before testing and query the VM's current IP:

```bash
./scheduler.sh pause
./scheduler.sh status

gcloud compute instances describe hw5-app \
  --zone=us-central1-f --project=cs-528-508021 \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)'
```

Set `HW5_IP` to the returned address and issue a request:

```bash
export HW5_IP=REPLACE_WITH_VM_IP

curl -i \
  -H 'X-country: Denmark' -H 'X-client-ip: 118.61.251.92' \
  -H 'X-gender: Female' -H 'X-age: 26-35' -H 'X-income: 40k-60k' \
  -H 'X-time: 2026-10-24 03:00:00' \
  "http://$HW5_IP:8080/10.html"
```

An existing file requested from an allowed country returns `200`, a banned country such as Cuba returns `400` and publishes a notification, and a missing file requested from an allowed country returns `404`.

For the full experiment, open two terminals, set `HW5_IP` in each, and run this command in both with the same seed:

```bash
./http-client \
  -d "$HW5_IP" -p 8080 -b none -w none \
  -n 50000 -i 11999 -r 42 -t 120
```

The supplied `http-client` binary is for macOS ARM64; use `http-client_linux` for Linux x86-64 or `http-client.exe` for Windows, and adjust `-i` if the verified file range differs from `0` through `11999`.

Start both clients close together and let each finish naturally, saving separate logs, exit codes, start/end times, and database counts before and after the run; use the supplied client directly because the retained `stress/run_clients.py` does not implement the required identical-seed, 50,000-request runs.

Monitor VM load, database connections, and Pub/Sub backlog during the experiment, leaving the subscriber code unchanged.

### Database schema

`service1/database.py` uses the Cloud SQL Python Connector with a reusable SQLAlchemy connection pool, configured through `INSTANCE_CONNECTION_NAME`, `DB_USER`, `DB_PASS`, `DB_NAME`, and optional pool settings.

The `requests` table stores every request:

| Column | SQL type | Meaning |
|---|---|---|
| `id` | `VARCHAR(36)`, primary key | Application-generated request UUID. |
| `country` | `TEXT` | Country from the request header. |
| `client_ip` | `TEXT` | Client IP header, falling back to the connection address. |
| `gender` | `TEXT` | Client-supplied gender. |
| `age` | `TEXT` | Age group, such as `26-35`. |
| `income` | `TEXT` | Income group, such as `40k-60k`. |
| `client_time` | `TEXT` | Raw `X-time` header, preserved without assuming a timezone. |
| `is_banned` | `BOOLEAN` | Whether the country is banned. |
| `requested_at` | `TIMESTAMP WITH TIME ZONE` | Server-recorded request time in UTC. |
| `requested_file` | `TEXT` | Requested file name. |
| `status_code` | `INTEGER` | HTTP status submitted for persistence. |
| `experiment_id` | `TEXT`, indexed | Experiment label from the server configuration. |

The `failed_requests` table stores an additional row for each non-200 request:

| Column | SQL type | Meaning |
|---|---|---|
| `id` | `VARCHAR(36)`, primary key | Same request UUID as the main row. |
| `requested_at` | `TIMESTAMP WITH TIME ZONE` | Same UTC request time as the main row. |
| `requested_file` | `TEXT` | Requested file name. |
| `error_code` | `INTEGER` | Non-200 HTTP status code. |

Both inserts run in one transaction, so a successful request normally adds one main row and an unsuccessful request adds one main row plus one error row.

If persistence fails, the server explicitly logs the attempted record and returns `503`; a failure during commit can leave the commit outcome uncertain, so inserts are not blindly retried.

The schema service runs `setup_schema.py` automatically; for manual setup with ADC and the database environment variables already configured, run:

```bash
python -m pip install -r requirements-db.txt
python setup_schema.py
```

Schema setup creates missing tables without deleting rows and is not a migration tool for changing existing table definitions.

### Timing and reports

The server uses `time.perf_counter_ns()` to instrument separate operations:

| Stage | Implementation | Measurement boundary |
|---|---|---|
| `request_parse` | `parse_request()` | Reading, validating, and normalizing headers and the file name. |
| `gcs_read` | `download_file()` | Downloading file content from Cloud Storage. |
| `database_write` | `write_request_record()` | Connection acquisition, inserts, and transaction commit. |
| `response_output` | `TimedResponse` in `service1/timing.py` | WSGI response iteration through closure, including waits while the HTTP server consumes output. |

Response output timing is a server-side measurement, not the client's end-to-end download time; synchronous Cloud Logging, Pub/Sub notifications, and other processing can contribute time outside these four stages.

On the Linux VM, inspect the service output with:

```bash
sudo journalctl -u hw5-web.service --no-pager -o cat
```

Export and download timing records before deleting the VM, checking for journal rotation, missing request IDs, or timing JSON concatenated with access logs; preserve the original export and extract complete JSON objects into a separate JSONL file when necessary.

Summarize the downloaded timing file locally using the experiment ID assigned during setup:

```bash
python analysis/summarize_timings.py \
  report/load-test/timings-clean.jsonl \
  --experiment-id hw5-load-01
```

The output reports milliseconds, valid sample counts, means, nearest-rank P50/P95, maxima, and stage outcomes; skipped GCS reads are excluded rather than counted as zero, and completed-only statistics are also available.

Use Cloud SQL Studio to query `requests` for success/failure totals, banned-country counts, gender counts, top countries, and age/income distributions, clearly stating whether preliminary requests are included.

### Shutdown and cleanup

The authenticated Scheduler job invokes the shutdown function every hour in UTC; pausing the job does not cancel an invocation already in progress.

```bash
./scheduler.sh resume
./scheduler.sh status
```

After exporting reports and logs, clean up the deployment:

```bash
./cleanup.sh
```

Cleanup removes the VM and related HW5 resources, releases the reserved static IP, and stops Cloud SQL with `activationPolicy=NEVER`, while preserving the SQL instance, its data, and the reused HW2 bucket.

If a temporary `hw5-iap-ssh` firewall rule was added manually for log export, delete it before cleanup so it does not prevent network removal.

Stopped Cloud SQL storage and IP resources, as well as retained bucket contents, can still incur charges; stopping the deployment does not guarantee a zero bill.
