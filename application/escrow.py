import uuid
import threading
from enum import Enum


class EscrowStatus(Enum):
    RESERVED = "RESERVED"
    RELEASED = "RELEASED"
    REFUNDED = "REFUNDED"


class MockEscrow:
    def __init__(self):
        self.transactions = {}
        self.lock = threading.RLock()

    def reserve_funds(self, bidder, auction_id, amount):
        """
        Mock reservation of bidder's funds.
        No real money is transferred.
        """
        transaction_id = str(uuid.uuid4())

        with self.lock:
            self.transactions[transaction_id] = {
                "transaction_id": transaction_id,
                "auction_id": auction_id,
                "bidder": bidder,
                "amount": amount,
                "status": EscrowStatus.RESERVED.value
            }

        return True, transaction_id

    def release_funds(self, auction_id, winner):
        """
        Mock release of funds to the seller after auction closes.
        """

        with self.lock:
            for transaction in self.transactions.values():

                if (
                    transaction["auction_id"] == auction_id
                    and transaction["bidder"] == winner
                    and transaction["status"] == EscrowStatus.RESERVED.value
                ):
                    transaction["status"] = EscrowStatus.RELEASED.value

                    return True, transaction["transaction_id"]

        return False, None

    def refund_funds(self, auction_id, bidder):
        """
        Mock refund.
        """

        with self.lock:
            for transaction in self.transactions.values():

                if (
                    transaction["auction_id"] == auction_id
                    and transaction["bidder"] == bidder
                    and transaction["status"] == EscrowStatus.RESERVED.value
                ):
                    transaction["status"] = EscrowStatus.REFUNDED.value

                    return True, transaction["transaction_id"]

        return False, None

    def get_transaction(self, transaction_id):

        with self.lock:
            return self.transactions.get(transaction_id)

    def get_auction_transactions(self, auction_id):

        with self.lock:
            return [
                transaction
                for transaction in self.transactions.values()
                if transaction["auction_id"] == auction_id
            ]