import grpc

from generated import llm_pb2
from generated import llm_pb2_grpc


class LLMClient:
    """
    Client used by the Application Server to communicate
    with LLM Server.
    """

    def __init__(self, address="localhost:50052"):
        self.channel = grpc.insecure_channel(address)

        self.stub = llm_pb2_grpc.LLMServiceStub(
            self.channel
        )

    def get_answer(
        self,
        request_id,
        task_type,
        query,
        auction_context=None,
        requester_context=None,
        conversation_history=None,
        additional_context="",
    ):
        request = llm_pb2.LLMRequest(
            request_id=request_id,
            task_type=task_type,
            query=query,
            additional_context=additional_context,
        )

        if auction_context is not None:
            request.auction_context.CopyFrom(
                auction_context
            )

        if requester_context is not None:
            request.requester_context.CopyFrom(
                requester_context
            )

        if conversation_history:
            request.conversation_history.extend(
                conversation_history
            )

        response = self.stub.GetLLMAnswer(request)

        return response