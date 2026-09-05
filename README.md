# untis

Two small tools built on top of [python-webuntis](https://python-webuntis.readthedocs.io/) for probing and dumping data from a WebUntis school-timetable account.

- **`main.py`** — logs in with the configured account and prints/exports all WebUntis data it can currently access (timetables, classes, teachers, rooms, subjects).
- **`test_permissions.py`** — walks every public endpoint exposed by the installed `python-webuntis` version and writes a JSON report of what succeeds, what fails with a permission error, and what fails for other reasons — useful for figuring out exactly what a given account is allowed to see.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install python-webuntis requests python-dotenv
cp .env.example .env              # then fill in your own credentials
```

`.env` (not committed — see `.env.example` for the expected keys):

```
UNTIS_USER=
UNTIS_PASSWORD=
UNTIS_SERVER=
UNTIS_SCHOOL=
```

## Usage

```bash
python main.py
python test_permissions.py
```

Both scripts write their own JSON output locally; neither file is committed since the content is account-specific.
