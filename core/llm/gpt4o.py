from openai import AsyncOpenAI

from config.settings import max_output_tokens

MODEL = "gpt-4o"


def create_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, max_retries=0, timeout=60.0)


async def generate(client: AsyncOpenAI, goal: str):
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are YIMI's single AI assistant. Answer the user's request "
                    "concisely and safely. You have no tools and cannot execute "
                    "external actions, make purchases, or promise financial returns."
                ),
            },
            {"role": "user", "content": goal},
        ],
        max_tokens=max_output_tokens(),
    )
    if not response.usage or not response.choices:
        raise RuntimeError("GPT-4o response did not include usage and a completion")
    answer = response.choices[0].message.content
    if not answer:
        raise RuntimeError("GPT-4o returned an empty completion")
    return answer, response.usage.prompt_tokens, response.usage.completion_tokens
