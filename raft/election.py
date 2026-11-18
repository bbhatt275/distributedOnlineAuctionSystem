import threading
import time

import grpc

from raft.raft_node import FOLLOWER, CANDIDATE, LEADER
from generated import raft_pb2
from generated import raft_pb2_grpc


class ElectionManager:
    def __init__(self, node):
        self.node = node
        self._started = False
        self._start_lock = threading.Lock()

    def start(self):
        with self._start_lock:
            if self._started:
                return
            self._started = True

        threading.Thread(target=self._election_loop, daemon=True).start()
        for peer_id, address in self.node.peers.items():
            threading.Thread(
                target=self._heartbeat_worker,
                args=(peer_id, address),
                daemon=True,
            ).start()

    def _heartbeat_worker(self, peer_id, address):
        channel = grpc.insecure_channel(address)
        stub = raft_pb2_grpc.RaftServiceStub(channel)
        try:
            while self.node.running:
                request = None
                request_term = None

                with self.node.lock:
                    if self.node.role == LEADER:
                        next_idx = self.node.next_index.get(
                            peer_id, len(self.node.log) + 1
                        )
                        next_idx = max(1, min(next_idx, len(self.node.log) + 1))
                        prev_idx = next_idx - 1
                        prev_term = (
                            self.node.log[prev_idx - 1]["term"]
                            if prev_idx > 0 else 0
                        )
                        entries = [
                            raft_pb2.LogEntry(
                                term=item["term"],
                                command=item["command"],
                            )
                            for item in self.node.log[next_idx - 1:]
                        ]
                        request_term = self.node.current_term
                        request = raft_pb2.AppendEntriesRequest(
                            term=request_term,
                            leader_id=self.node.node_id,
                            prev_log_index=prev_idx,
                            prev_log_term=prev_term,
                            entries=entries,
                            leader_commit=self.node.commit_index,
                        )

                if request is not None:
                    try:
                        response = stub.AppendEntries(request, timeout=0.75)
                    except grpc.RpcError:
                        time.sleep(0.1)
                        continue

                    with self.node.lock:
                        if response.term > self.node.current_term:
                            self.node.current_term = response.term
                            self.node.role = FOLLOWER
                            self.node.voted_for = None
                            self.node.leader_id = None
                            self.node.reset_election_timeout()
                            self.node.persist()
                            self.node.commit_condition.notify_all()
                        elif (
                            self.node.role == LEADER
                            and self.node.current_term == request_term
                        ):
                            if response.success:
                                matched = max(0, response.match_index)
                                self.node.match_index[peer_id] = max(
                                    self.node.match_index.get(peer_id, 0), matched
                                )
                                self.node.next_index[peer_id] = (
                                    self.node.match_index[peer_id] + 1
                                )
                            else:
                                self.node.next_index[peer_id] = max(
                                    1,
                                    self.node.next_index.get(peer_id, 1) - 1,
                                )

                    self._advance_commit_index()

                time.sleep(0.1)
        finally:
            channel.close()

    def _election_loop(self):
        while self.node.running:
            time.sleep(0.05)
            with self.node.lock:
                if self.node.role == LEADER:
                    continue
                elapsed = time.monotonic() - self.node.last_heartbeat
                if elapsed < self.node.election_timeout:
                    continue
            self.start_election()

    def start_election(self):
        with self.node.lock:
            if not self.node.running or self.node.role == LEADER:
                return

            self.node.current_term += 1
            term = self.node.current_term
            self.node.role = CANDIDATE
            self.node.leader_id = None
            self.node.voted_for = self.node.node_id
            self.node.votes_received = {self.node.node_id}
            self.node.reset_election_timeout()
            self.node.persist()
            self.node.commit_condition.notify_all()

            last_log_index = len(self.node.log)
            last_log_term = self.node.log[-1]["term"] if self.node.log else 0
            print(
                f"[Node {self.node.node_id}] Starting election for term {term}"
            )
            won_alone = len(self.node.votes_received) >= self._majority()

        if won_alone:
            self.become_leader(term)
            return

        for peer_id, address in self.node.peers.items():
            threading.Thread(
                target=self._request_vote,
                args=(peer_id, address, term, last_log_index, last_log_term),
                daemon=True,
            ).start()

    def _request_vote(
        self, peer_id, address, term, last_log_index, last_log_term
    ):
        channel = grpc.insecure_channel(address)
        stub = raft_pb2_grpc.RaftServiceStub(channel)
        try:
            response = stub.RequestVote(
                raft_pb2.RequestVoteRequest(
                    term=term,
                    candidate_id=self.node.node_id,
                    last_log_index=last_log_index,
                    last_log_term=last_log_term,
                ),
                timeout=1.0,
            )

            with self.node.lock:
                if response.term > self.node.current_term:
                    self.node.current_term = response.term
                    self.node.role = FOLLOWER
                    self.node.voted_for = None
                    self.node.leader_id = None
                    self.node.reset_election_timeout()
                    self.node.persist()
                    self.node.commit_condition.notify_all()
                    return

                if (
                    self.node.role != CANDIDATE
                    or self.node.current_term != term
                ):
                    return

                if response.vote_granted:
                    self.node.votes_received.add(peer_id)
                    print(
                        f"[Node {self.node.node_id}] Received vote from Node {peer_id}"
                    )
                    won = len(self.node.votes_received) >= self._majority()
                else:
                    won = False

            if won:
                self.become_leader(term)
        except grpc.RpcError:
            print(f"[Node {self.node.node_id}] Could not contact Node {peer_id}")
        finally:
            channel.close()

    def become_leader(self, term):
        with self.node.lock:
            if (
                self.node.role != CANDIDATE
                or self.node.current_term != term
            ):
                return

            self.node.role = LEADER
            self.node.leader_id = self.node.node_id
            self.node.next_index = {
                peer_id: len(self.node.log) + 1
                for peer_id in self.node.peers
            }
            self.node.match_index = {peer_id: 0 for peer_id in self.node.peers}
            self.node.commit_condition.notify_all()
            print(
                f"\n[Node {self.node.node_id}] BECAME LEADER "
                f"for term {term}\n"
            )

    def _majority(self):
        return (len(self.node.peers) + 1) // 2 + 1

    def propose_command(self, command, timeout=5.0):
        """Append a command and wait until it is committed and applied locally."""
        with self.node.lock:
            if self.node.role != LEADER:
                raise RuntimeError("This node is not the Raft leader")

            proposal_term = self.node.current_term
            self.node.log.append({
                "term": proposal_term,
                "command": bytes(command),
            })
            index = len(self.node.log)
            self.node.persist()

        # Also handles a one-node cluster; for five nodes replication workers
        # will advance the commit index when a majority acknowledges the entry.
        self._advance_commit_index()

        deadline = time.monotonic() + timeout
        with self.node.commit_condition:
            while self.node.last_applied < index:
                if (
                    self.node.role != LEADER
                    or self.node.current_term != proposal_term
                ):
                    raise RuntimeError(
                        "Leadership changed before the command was committed"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Commit timeout for log index {index}")
                self.node.commit_condition.wait(remaining)

        return index

    def _advance_commit_index(self):
        advanced = False
        with self.node.lock:
            if self.node.role != LEADER:
                return

            majority = self._majority()
            for index in range(
                len(self.node.log), self.node.commit_index, -1
            ):
                # A leader only directly commits entries from its current term.
                if self.node.log[index - 1]["term"] != self.node.current_term:
                    continue

                replicated = 1  # The leader itself.
                for peer_id in self.node.peers:
                    if self.node.match_index.get(peer_id, 0) >= index:
                        replicated += 1

                if replicated >= majority:
                    self.node.commit_index = index
                    self.node.persist()
                    self.node.commit_condition.notify_all()
                    advanced = True
                    print(
                        f"[Node {self.node.node_id}] Committed log index {index}"
                    )
                    break

        if advanced:
            self.node.apply_committed_entries()
