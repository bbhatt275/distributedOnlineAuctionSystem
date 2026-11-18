import os
import sqlite3
import threading


class RaftStorage:
    """Durable storage for Raft term, vote, log, and commit index."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()

        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS raft_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    current_term INTEGER NOT NULL,
                    voted_for TEXT,
                    commit_index INTEGER NOT NULL DEFAULT 0
                )
            """)

            # Migrate databases created by the earlier implementation.
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(raft_meta)")
            }
            if "commit_index" not in columns:
                conn.execute(
                    "ALTER TABLE raft_meta ADD COLUMN commit_index INTEGER NOT NULL DEFAULT 0"
                )

            conn.execute("""
                INSERT OR IGNORE INTO raft_meta
                    (id, current_term, voted_for, commit_index)
                VALUES (1, 0, NULL, 0)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS raft_log (
                    log_index INTEGER PRIMARY KEY,
                    term INTEGER NOT NULL,
                    command BLOB NOT NULL
                )
            """)

    def load_state(self):
        with self.lock, sqlite3.connect(self.db_path, timeout=10) as conn:
            meta = conn.execute("""
                SELECT current_term, voted_for, commit_index
                FROM raft_meta WHERE id = 1
            """).fetchone()
            rows = conn.execute("""
                SELECT term, command
                FROM raft_log ORDER BY log_index
            """).fetchall()

        log = [
            {"term": term, "command": bytes(command)}
            for term, command in rows
        ]
        commit_index = min(int(meta[2]), len(log))
        return {
            "current_term": int(meta[0]),
            "voted_for": meta[1],
            "commit_index": commit_index,
            "log": log,
        }

    def save_state(self, current_term, voted_for, log, commit_index=0):
        commit_index = min(max(0, int(commit_index)), len(log))
        with self.lock, sqlite3.connect(self.db_path, timeout=10) as conn:
            with conn:
                conn.execute("""
                    UPDATE raft_meta
                    SET current_term = ?, voted_for = ?, commit_index = ?
                    WHERE id = 1
                """, (current_term, voted_for, commit_index))

                conn.execute("DELETE FROM raft_log")
                conn.executemany("""
                    INSERT INTO raft_log (log_index, term, command)
                    VALUES (?, ?, ?)
                """, [
                    (index, entry["term"], sqlite3.Binary(entry["command"]))
                    for index, entry in enumerate(log, start=1)
                ])
