import threading


class StateStore:

    def __init__(self):
        self.auctions = {}
        self.bids = {}
        self.lock = threading.RLock()