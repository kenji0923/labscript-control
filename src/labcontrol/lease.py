"""PC-wide cooperative leases and mutation serialization across processes/apps.

The SQLite transaction gates admission and synchronous command execution. A lease
also reserves the machine between commands and across normal app restarts. GUI
buttons and third-party native APIs are outside this cooperative protocol.
"""
from contextlib import contextmanager
import hashlib
import sqlite3
import time


class LeaseError(RuntimeError):
    pass


class LeaseGate:
    def __init__(self, directory):
        self.database = directory / 'control-lease.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS lease '
                       '(singleton INTEGER PRIMARY KEY CHECK(singleton=1), token TEXT, owner TEXT, expires REAL)')

    def connect(self):
        return sqlite3.connect(self.database, timeout=2)

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def active(self, db):
        row = db.execute('SELECT token,owner,expires FROM lease WHERE singleton=1').fetchone()
        return row if row and row[2] > time.time() else None

    def status(self):
        with self.connect() as db:
            row = self.active(db)
        return {'held': bool(row), 'owner': row[1] if row else None,
                'expires_at': row[2] if row else None}

    @staticmethod
    def digest(token):
        if not isinstance(token, str) or not 16 <= len(token) <= 128:
            raise LeaseError('lease_id must contain 16..128 characters; use a UUID')
        return hashlib.sha256(token.encode()).hexdigest()

    def check(self, db, token):
        row = self.active(db)
        if row:
            if not token or self.digest(token) != row[0]:
                raise LeaseError(f'Machine is reserved by {row[1]} until {row[2]}; supply its lease_id')
        elif token:
            raise LeaseError('Lease has expired or was released; acquire a new lease before changing state')

    def control(self, db, action, params):
        token = params.get('lease_id')
        digest = self.digest(token)
        row = self.active(db)
        if action == 'lease.acquire':
            if row:
                raise LeaseError(f'Machine is already reserved by {row[1]}; renew the existing lease')
            owner = params.get('owner')
            if not isinstance(owner, str) or not 1 <= len(owner) <= 128:
                raise LeaseError('owner must contain 1..128 characters')
        elif action in ('lease.renew', 'lease.release'):
            if not row or row[0] != digest:
                raise LeaseError('Lease is absent, expired, or belongs to another owner')
            owner = row[1]
        else:
            raise LeaseError('Unknown lease action')
        if action == 'lease.release':
            db.execute('DELETE FROM lease WHERE singleton=1')
            return {'released': True}
        ttl = params.get('ttl', 300)
        if type(ttl) is not int or not 30 <= ttl <= 3600:
            raise LeaseError('ttl must be an integer from 30 to 3600 seconds')
        expires = time.time() + ttl
        db.execute('INSERT OR REPLACE INTO lease VALUES (1,?,?,?)', (digest, owner, expires))
        return {'lease_id': token, 'owner': owner, 'expires_at': expires}
