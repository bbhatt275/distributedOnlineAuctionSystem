import os

import ollama


# Override with LLM_MODEL to try a different one without editing code.
MODEL_NAME = os.environ.get("LLM_MODEL", "llama3.2:3b")

# qwen3.5 is a reasoning model. Left to itself it emits ~4000 thinking tokens
# before answering, which at ~22 tok/s is over three minutes per reply. The
# answers are no better for it, so thinking is off unless LLM_THINKING=1.
THINKING = os.environ.get("LLM_THINKING", "0") == "1"


class LLMModel:
    """
    Thin wrapper around the locally running Ollama model.

    This class is responsible ONLY for model inference.
    It does not know anything about:
    - gRPC
    - auctions
    - users
    - authentication
    - bidding
    """

    def __init__(self, model_name: str = MODEL_NAME):
        self.model_name = model_name

    def generate(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 0,
    ) -> str:
        """
        Send a chat request to Ollama and return the generated text.

        max_tokens=0 means use Ollama's default.
        """

        options = {
            "temperature": temperature
        }

        if max_tokens > 0:
            options["num_predict"] = max_tokens

        response = ollama.chat(
            model=self.model_name,
            messages=messages,
            options=options,
            think=THINKING,
        )

        return response["message"]["content"]