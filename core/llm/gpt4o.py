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
                    "You are YIMI's task orchestrator and single GPT-4o assistant. "
                    "Answer the request concisely and safely. You may suggest how "
                    "YIMI's local monitoring, security, resource, monetization, and "
                    "analytics modules could help, but do not claim to have called "
                    "them or observed data unless it is provided in the request. "
                    "You have no tools and cannot execute external actions, make "
                    "purchases, change infrastructure, or promise financial returns. "
                    "Request human review for consequential or irreversible actions."
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
