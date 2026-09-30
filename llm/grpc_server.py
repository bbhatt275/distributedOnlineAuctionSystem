import time

import grpc

from generated import llm_pb2
from generated import llm_pb2_grpc

from llm.generator import LLMGenerator


class LLMService(llm_pb2_grpc.LLMServiceServicer):
    """
    gRPC service for the independent LLM server.

    Responsibilities:
    - receive LLMRequest
    - validate the request
    - call LLMGenerator
    - return LLMResponse

    This service does not know anything about:
    - auction business logic
    - authentication
    - bidding
    - payment
    """

    def __init__(self):
        self.generator = LLMGenerator()

    def GetLLMAnswer(self, request, context):

        start_time = time.perf_counter()

        # Ensure the validity of the request

        if not request.request_id:
            return llm_pb2.LLMResponse(
                request_id="",
                success=False,
                message="request_id is required",
                error_code=llm_pb2.INVALID_REQUEST,
            )

        if request.task_type == llm_pb2.LLM_TASK_UNSPECIFIED:
            return llm_pb2.LLMResponse(
                request_id=request.request_id,
                success=False,
                message="task_type is required",
                error_code=llm_pb2.INVALID_REQUEST,
            )

        if not request.query:
            return llm_pb2.LLMResponse(
                request_id=request.request_id,
                success=False,
                message="query is required",
                error_code=llm_pb2.INVALID_REQUEST,
            )

        # Generate answer

        try:

            answer = self.generator.generate(request)

            processing_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return llm_pb2.LLMResponse(
                request_id=request.request_id,
                success=True,
                answer=answer,
                message="LLM answer generated successfully",
                error_code=llm_pb2.LLM_ERROR_NONE,
                model_name=self.generator.model.model_name,
                processing_time_ms=processing_time_ms,
            )

        # Model unavailable

        except Exception as e:

            processing_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            print(
                f"[LLM ERROR] request_id={request.request_id}: {e}"
            )

            return llm_pb2.LLMResponse(
                request_id=request.request_id,
                success=False,
                message="LLM inference failed",
                error_code=llm_pb2.MODEL_INFERENCE_FAILED,
                model_name=self.generator.model.model_name,
                processing_time_ms=processing_time_ms,
            )