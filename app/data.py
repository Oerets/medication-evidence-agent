"""Lossless local CSV ingestion. No remote services or clinical inference."""
import csv
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
RAW = ROOT / CONFIG['raw_dir']
DB = ROOT / CONFIG['database']
TABLES = ['hosp/patients', 'hosp/admissions', 'hosp/prescriptions', 'hosp/emar',
          'hosp/emar_detail', 'hosp/diagnoses_icd', 'hosp/d_icd_diagnoses',
          'hosp/labevents', 'hosp/d_labitems', 'icu/icustays', 'icu/d_items',
          'icu/chartevents', 'icu/inputevents']


def connect(path=None):
    conn = sqlite3.connect(path or DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def table_name(name):
    return name.replace('/', '_')


def initialize(conn):
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS prepared (
        subject_id TEXT NOT NULL, cutoff TEXT NOT NULL, version TEXT NOT NULL,
        prepared_at TEXT NOT NULL, bundle TEXT NOT NULL,
        PRIMARY KEY(subject_id, cutoff, version));
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, state TEXT NOT NULL, total INTEGER NOT NULL,
        done INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS job_items (
        job_id TEXT NOT NULL, subject_id TEXT NOT NULL, state TEXT NOT NULL,
        error TEXT, PRIMARY KEY(job_id, subject_id));
      CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY, subject_id TEXT NOT NULL, drug TEXT NOT NULL,
        cutoff TEXT NOT NULL, evidence_version TEXT NOT NULL,
        decision TEXT NOT NULL, comment TEXT NOT NULL, created_at TEXT NOT NULL);
    ''')


def verify_raw():
    checks = []
    for line in (RAW / 'SHA256SUMS.txt').read_text().splitlines():
        expected, filename = line.split(maxsplit=1)
        path = (RAW / filename.lstrip('*').removeprefix('./')).resolve()
        if not path.is_relative_to(RAW.resolve()):
            raise ValueError('Checksum path outside dataset')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise ValueError(f'Checksum mismatch: {filename}')
        checks.append({'file': filename, 'sha256': digest})
    return checks


def ingest():
    checks = verify_raw()
    fingerprint = hashlib.sha256(json.dumps(checks, sort_keys=True).encode()).hexdigest()
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        initialize(conn)
        old = conn.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()
        if old and json.loads(old[0]) == fingerprint:
            print('Dataset already imported and checksums verified.')
            return
        inventory = []
        with conn:
            for name in TABLES:
                table = table_name(name)
                with gzip.open(RAW / (name + '.csv.gz'), 'rt', encoding='utf-8', newline='') as f:
                    reader = csv.DictReader(f)
                    columns = reader.fieldnames
                    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
                    fields = ', '.join(f'"{col}" TEXT' for col in columns)
                    conn.execute(f'CREATE TABLE "{table}" (source_row INTEGER PRIMARY KEY, {fields})')
                    sql = f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in range(len(columns)+1))})'
                    batch, count = [], 0
                    for count, row in enumerate(reader, 1):
                        batch.append((count, *(row[col] for col in columns)))
                        if len(batch) == 5000:
                            conn.executemany(sql, batch)
                            batch.clear()
                    if batch:
                        conn.executemany(sql, batch)
                    for col in ['subject_id', 'hadm_id', 'itemid', 'emar_id']:
                        if col in columns:
                            conn.execute(f'CREATE INDEX "idx_{table}_{col}" ON "{table}" ("{col}")')
                    if 'subject_id' in columns and 'charttime' in columns:
                        conn.execute(f'CREATE INDEX "idx_{table}_time" ON "{table}" (subject_id, charttime)')
                    inventory.append({'table': table, 'file': name+'.csv.gz', 'rows': count, 'columns': columns})
                    print(f'{table}: {count:,} rows', flush=True)
            conn.execute('DELETE FROM prepared')
            meta = {'fingerprint': fingerprint, 'inventory': inventory, 'checksums_verified': len(checks),
                    'dataset': CONFIG['dataset'], 'version': CONFIG['version'], 'notes_available': False}
            for key, value in meta.items():
                conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))
    finally:
        conn.close()


if __name__ == '__main__':
    ingest()
