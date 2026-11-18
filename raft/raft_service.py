import grpc

from raft.raft_node import FOLLOWER
from generated import raft_pb2
from generated import raft_pb2_grpc


class RaftService(raft_pb2_grpc.RaftServiceServicer):
    def __init__(self, node):
        self.node = node

    def AppendEntries(self, request, context):
        should_apply = False

        with self.node.lock:
            if request.term < self.node.current_term:
                return raft_pb2.AppendEntriesResponse(
                    term=self.node.current_term,
                    success=False,
                    match_index=0,
                )

            term_changed = request.term > self.node.current_term
            if term_changed:
                self.node.current_term = request.term
                self.node.voted_for = None

            # A valid AppendEntries from this term makes the node a follower.
            self.node.role = FOLLOWER
            self.node.leader_id = request.leader_id
            self.node.reset_election_timeout()

            if request.prev_log_index > len(self.node.log):
                if term_changed:
                    self.node.persist()
                    self.node.commit_condition.notify_all()
                return raft_pb2.AppendEntriesResponse(
                    term=self.node.current_term,
                    success=False,
                    match_index=0,
                )

            if request.prev_log_index > 0:
                previous = self.node.log[request.prev_log_index - 1]
                if previous["term"] != request.prev_log_term:
                    if term_changed:
                        self.node.persist()
                        self.node.commit_condition.notify_all()
                    return raft_pb2.AppendEntriesResponse(
                        term=self.node.current_term,
                        success=False,
                        match_index=0,
                    )

            log_changed = False
            for offset, incoming in enumerate(request.entries):
                index = request.prev_log_index + offset + 1
                if index <= len(self.node.log):
                    existing = self.node.log[index - 1]
                    if existing["term"] != incoming.term:
                        # A committed entry must never be overwritten.
                        if index <= self.node.commit_index:
                            context.abort(
                                grpc.StatusCode.FAILED_PRECONDITION,
                                "Leader attempted to overwrite a committed log entry",
                            )
                        self.node.log = self.node.log[:index - 1]
                        for remaining in request.entries[offset:]:
                            self.node.log.append({
                                "term": remaining.term,
                                "command": bytes(remaining.command),
                            })
                        log_changed = True
                        break
                else:
                    self.node.log.append({
                        "term": incoming.term,
                        "command": bytes(incoming.command),
                    })
                    log_changed = True

            new_commit = min(request.leader_commit, len(self.node.log))
            if new_commit > self.node.commit_index:
                self.node.commit_index = new_commit
                should_apply = True

            if term_changed or log_changed or should_apply:
                self.node.persist()
            if term_changed or should_apply:
                self.node.commit_condition.notify_all()

            matched_index = request.prev_log_index + len(request.entries)
            response = raft_pb2.AppendEntriesResponse(
                term=self.node.current_term,
                success=True,
                match_index=matched_index,
            )

        # Never run application callbacks while holding the Raft state lock.
        if should_apply:
            self.node.apply_committed_entries()
        return response

    def RequestVote(self, request, context):
        with self.node.lock:
            if request.term < self.node.current_term:
                return raft_pb2.RequestVoteResponse(
                    term=self.node.current_term,
                    vote_granted=False,
                )

            term_changed = request.term > self.node.current_term
            if term_changed:
                self.node.current_term = request.term
                self.node.voted_for = None
                self.node.role = FOLLOWER
                self.node.leader_id = None

            last_log_index = len(self.node.log)
            last_log_term = self.node.log[-1]["term"] if self.node.log else 0
            candidate_up_to_date = (
                request.last_log_term > last_log_term
                or (
                    request.last_log_term == last_log_term
                    and request.last_log_index >= last_log_index
                )
            )
            can_vote = (
                self.node.voted_for is None
                or self.node.voted_for == request.candidate_id
            )
            vote_granted = can_vote and candidate_up_to_date

            if vote_granted:
                self.node.voted_for = request.candidate_id
                self.node.reset_election_timeout()
                print(
                    f"[Node {self.node.node_id}] Granted vote to Node "
                    f"{request.candidate_id} for term {request.term}"
                )

            self.node.persist()
            if term_changed:
                self.node.commit_condition.notify_all()

            return raft_pb2.RequestVoteResponse(
                term=self.node.current_term,
                vote_granted=vote_granted,
            )
