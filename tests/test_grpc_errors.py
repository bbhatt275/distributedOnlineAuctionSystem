import grpc

from generated import llm_pb2
from generated import llm_pb2_grpc


def test_missing_request_id(stub):

    request = llm_pb2.LLMRequest(
        task_type=llm_pb2.AUCTION_FAQ,
        query="What is the current highest bid?",
    )

    response = stub.GetLLMAnswer(request)

    print("\n===== TEST: MISSING REQUEST ID =====")
    print("Success:", response.success)
    print("Message:", response.message)
    print("Error:", response.error_code)

    assert response.success is False
    assert response.error_code == llm_pb2.INVALID_REQUEST


def test_missing_task_type(stub):

    request = llm_pb2.LLMRequest(
        request_id="error-test-002",
        query="What is the current highest bid?",
    )

    response = stub.GetLLMAnswer(request)

    print("\n===== TEST: MISSING TASK TYPE =====")
    print("Success:", response.success)
    print("Message:", response.message)
    print("Error:", response.error_code)

    assert response.success is False
    assert response.error_code == llm_pb2.INVALID_REQUEST


def test_missing_query(stub):

    request = llm_pb2.LLMRequest(
        request_id="error-test-003",
        task_type=llm_pb2.AUCTION_FAQ,
    )

    response = stub.GetLLMAnswer(request)

    print("\n===== TEST: MISSING QUERY =====")
    print("Success:", response.success)
    print("Message:", response.message)
    print("Error:", response.error_code)

    assert response.success is False
    assert response.error_code == llm_pb2.INVALID_REQUEST


def main():

    channel = grpc.insecure_channel(
        "localhost:50052"
    )

    stub = llm_pb2_grpc.LLMServiceStub(channel)

    test_missing_request_id(stub)
    test_missing_task_type(stub)
    test_missing_query(stub)

    print("\nAll gRPC error tests passed.")


if __name__ == "__main__":
    main()