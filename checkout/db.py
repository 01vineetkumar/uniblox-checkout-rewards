import sqlite3
import time
from contextlib import contextmanager
from os import fspath
from pathlib import Path

from checkout.errors import DomainError

SEED_PRODUCTS = [
    ("p1", "Notebook", 1299, 100),
    ("p2", "Pen set", 599, 100),
    ("p3", "Desk lamp", 3499, 25),
    ("p4", "Travel mug", 1899, 50),
    ("p5", "Limited edition backpack", 7999, 1),
]


def is_lock_error(exc):
    return getattr(exc, "sqlite_errorcode", 0) & 0xFF in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    )


class Database:
    def __init__(self, path: str, reward_every: int = 5, discount_percent: int = 10):
        if type(reward_every) is not int or not 1 <= reward_every <= 2**63 - 1:
            raise ValueError("REWARD_EVERY must be an integer from 1 to 9223372036854775807")
        if type(discount_percent) is not int or not 1 <= discount_percent <= 100:
            raise ValueError("DISCOUNT_PERCENT must be an integer between 1 and 100")
        path = fspath(path)
        if not isinstance(path, str) or not path.strip():
            raise ValueError("DATABASE_PATH must be a nonempty filesystem path")
        if path == ":memory:":
            raise ValueError("Use a file database; operations use independent connections")
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 5
        while True:
            try:
                self._initialize(reward_every, discount_percent)
                break
            except sqlite3.OperationalError as exc:
                # WAL mode transitions can report BUSY immediately despite busy_timeout.
                # Independent workers must be able to start against a fresh shared file.
                if not is_lock_error(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _initialize(self, reward_every, discount_percent):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            conn.executescript("BEGIN IMMEDIATE;\n" + schema)
            conn.execute(
                "INSERT OR IGNORE INTO settings VALUES (1, ?, ?)",
                (reward_every, discount_percent),
            )
            saved = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
            if (saved["reward_every"], saved["discount_percent"]) != (
                reward_every,
                discount_percent,
            ):
                raise ValueError("Reward settings differ from this database's saved settings")
            conn.executemany("INSERT OR IGNORE INTO products VALUES (?, ?, ?, ?)", SEED_PRODUCTS)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            return conn
        except BaseException:
            conn.close()
            raise

    @contextmanager
    def transaction(self, write=False):
        conn = None
        try:
            conn = self.connect()
            # Acquire the writer lock BEFORE reading state used to make a decision.
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.rollback()
            if is_lock_error(exc):
                raise DomainError(503, "database_busy", "Database is busy; retry later") from exc
            raise
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()
