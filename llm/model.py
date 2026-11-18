import os
import ollama

MODEL_NAME = os.environ.get("LLM_MODEL", "llama3.2:3b")
THINKING = os.environ.get("LLM_THINKING", "0") == "1"

OLLAMA_HOST = os.environ.get(
    "OLLAMA_HOST",
    "http://localhost:11434"
)

class LLMModel:
    """
    Thin wrapper around the locally running Ollama model.

    This class is responsible ONLY for model inference.
    It does not know anything about gRPC, auctions, users, authentication or bidding.
    """


    def __init__(self, model_name: str = MODEL_NAME):
        self.model_name = model_name
        self.client = ollama.Client(host=OLLAMA_HOST)

    def generate(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 0,
    ) -> str:
        """
        Send a chat request to Ollama and return the generated text.
        max_tokens=0 means use Ollama's default if not provided.
        """

        options = {
            "temperature": temperature
        }

        if max_tokens > 0:
            options["num_predict"] = max_tokens

        response = self.client.chat(
            model=self.model_name,
            messages=messages,
            options=options,
        )

        return response["message"]["content"]