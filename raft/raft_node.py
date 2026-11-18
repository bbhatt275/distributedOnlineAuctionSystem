import os
import random
import threading
import time

from raft.persistence import RaftStorage

FOLLOWER = "FOLLOWER"
CANDIDATE = "CANDIDATE"
LEADER = "LEADER"


class RaftNode:
    def __init__(self, node_id, peers):
        self.node_id = str(node_id)
        self.peers = {str(k): v for k, v in peers.items()}

        db_path = os.getenv(
            "RAFT_DB_PATH",
            f"raft_data/node_{self.node_id}.db",
        )
        self.storage = RaftStorage(db_path)
        saved_state = self.storage.load_state()

        # Persistent state
        self.current_term = saved_state["current_term"]
        self.voted_for = saved_state["voted_for"]
        self.log = saved_state["log"]
        self.commit_index = saved_state.get("commit_index", 0)

        # Volatile state. Replay committed entries after the callback is set.
        self.last_applied = 0
        self.next_index = {}
        self.match_index = {}

        self.role = FOLLOWER
        self.leader_id = None
        self.votes_received = set()

        self.lock = threading.RLock()
        self.commit_condition = threading.Condition(self.lock)
        self.apply_lock = threading.Lock()

        self.election_timeout = self._random_timeout()
        self.last_heartbeat = time.monotonic()
        self.running = False
        self.apply_callback = None

    def set_apply_callback(self, callback):
        self.apply_callback = callback
        # Rebuild the in-memory state machine from the durable committed log.
        self.apply_committed_entries()

    def _random_timeout(self):
        return random.uniform(1.5, 3.0)

    def reset_election_timeout(self):
        with self.lock:
            self.election_timeout = self._random_timeout()
            self.last_heartbeat = time.monotonic()

    def get_state(self):
        with self.lock:
            return {
                "node_id": self.node_id,
                "term": self.current_term,
                "role": self.role,
                "leader_id": self.leader_id,
                "voted_for": self.voted_for,
                "commit_index": self.commit_index,
                "last_applied": self.last_applied,
            }

    def persist(self):
        with self.lock:
            self.storage.save_state(
                self.current_term,
                self.voted_for,
                self.log,
                self.commit_index,
            )

    def apply_committed_entries(self):
        """Apply committed entries in log order to this node's state machine."""
        if self.apply_callback is None:
            return

        with self.apply_lock:
            while True:
                with self.lock:
                    if self.last_applied >= self.commit_index:
                        return
                    index = self.last_applied
                    if index >= len(self.log):
                        print(
                            f"[Node {self.node_id}] commit index exceeds log length; "
                            "application paused"
                        )
                        return
                    command = self.log[index]["command"]

                try:
                    self.apply_callback(command)
                except Exception as exc:
                    print(
                        f"[Node {self.node_id}] Failed to apply log index "
                        f"{index + 1}: {exc}"
                    )
                    return

                with self.lock:
                    self.last_applied += 1
                    self.commit_condition.notify_all()
