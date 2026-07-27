import random

import avalanche as ava

# __AGENT_LM_SETUP__

@ava.source
def generate_binary() -> str:
    length = random.randint(128, 256)
    return "1" + "".join(random.choice("01") for _ in range(length - 1))


@ava.agent_step(
    ava.Signature("binary: str -> decimal: str"),
    lm="__AGENT_LM__",
)
async def convert_binary(binary: str, *, agent: ava.Agent) -> str:
    return (await agent(binary=binary)).decimal


@ava.dest
def print_result(result: str) -> str:
    print(result)
    return result


@ava.workflow
def binary_converter():
    return generate_binary() >> convert_binary() >> print_result()
