from llm.model import LLMModel
from llm.context_builder import (
    build_messages,
    get_system_prompt,
)


class LLMGenerator:
    """
    Coordinates task selection, prompt construction and model inference

    This class does not know anything about gRPC, rather it acts as a service layer only.
    """

    def __init__(self, model=None):
        self.model = model or LLMModel()

    def generate(self, request):

        system_prompt = get_system_prompt(
            request.task_type
        )

        user_messages = build_messages(request)

        messages = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]

        messages.extend(user_messages)

        temperature = 0.2 #default:low creativity more deterministic result
        max_tokens = 0 #unlimited/model default

        if request.HasField("generation_config"):

            if request.generation_config.temperature > 0:
                temperature = (
                    request.generation_config.temperature
                )

            if request.generation_config.max_tokens > 0:
                max_tokens = (
                    request.generation_config.max_tokens
                )

        answer = self.model.generate(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        return answer