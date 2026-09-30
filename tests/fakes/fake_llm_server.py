"""Stand-in for the LLM node, so the client chain can be tested without Ollama.

    python -m tests.fakes.fake_llm_server

Serves llm.LLMService on :50052 and answers from the AuctionContext the
application server sends. The answers are fixed, so this confirms that the
calls are connected correctly rather than saying anything about the model.
The real server in llm/ should be used for anything else.
"""

from concurrent import futures

import grpc

from generated import llm_pb2, llm_pb2_grpc


def _describe(ctx):
    if not ctx or not ctx.auction_id:
        return "no auction context was attached"
    return (
        f"{ctx.item_name} (starting {ctx.starting_price:.2f} {ctx.currency}), "
        f"currently {ctx.current_highest_bid:.2f} from "
        f"{ctx.highest_bidder or 'nobody'} across {ctx.bid_count} bid(s); "
        f"{'open' if ctx.active else 'closed'}"
    )


class FakeLLM(llm_pb2_grpc.LLMServiceServicer):

    def GetLLMAnswer(self, request, context):
        task = llm_pb2.LLMTaskType.Name(request.task_type)
        who = request.requester_context.user_id or "unknown user"

        answer = (
            f"[fake LLM] task={task}, asked by {who}.\n"
            f"Question: {request.query}\n"
            f"Auction: {_describe(request.auction_context)}"
        )

        return llm_pb2.LLMResponse(
            request_id=request.request_id,
            success=True,
            answer=answer,
            message="Success",
            error_code=llm_pb2.LLM_ERROR_NONE,
            model_name="fake-llm",
            processing_time_ms=1,
        )


def serve(port=50052):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    llm_pb2_grpc.add_LLMServiceServicer_to_server(FakeLLM(), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"fake LLM server on {port}")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
