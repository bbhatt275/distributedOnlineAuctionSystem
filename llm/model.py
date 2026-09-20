import ollama


MODEL_NAME = "llama3.2:3b"


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
        )

        return response["message"]["content"]