from llm.model import LLMModel


def main():
    model = LLMModel()

    messages = [
        {
            "role": "system",
            "content": (
                "You are an assistant for a distributed "
                "online auction system."
            ),
        },
        {
            "role": "user",
            "content": (
                "Explain what an online auction is "
                "in two short sentences."
            ),
        },
    ]

    answer = model.generate(messages)

    print("\n===== MODEL RESPONSE =====")
    print(answer)
    print("==========================\n")


if __name__ == "__main__":
    main()